### Copyright (C) 2019 NVIDIA Corporation. Ting-Chun Wang, Ming-Yu Liu, Jun-Yan Zhu.
### BSD License. All rights reserved.
###
### Copyright (c) 2019 Caroline Chan
### All rights reserved.
###
### Permission to use, copy, modify, in source and binary forms for non-commerical purposes,
### are permitted provided that the following conditions are met:
###
### * Modifications and copies, and must be for non-commericial purpose.
###
### * Modifications and copies in binary form must reproduce the above copyright notice,
###   this list of conditions and the following disclaimer in the documentation
###   and/or other materials provided with the distribution.
###
### * Redistribution of the software in source and binary forms is not permitted.
import numpy as np
import torch
import os
from collections import OrderedDict
from torch.autograd import Variable
import torch.nn as nn
import util.util as util
from util.image_pool import ImagePool
from models.base_model import BaseModel
from . import networks
import sys


class Pix2PixHDtsModel(BaseModel):
    def name(self):
        return 'Pix2PixHDtsModel'

    def initialize(self, opt):
        BaseModel.initialize(self, opt)
        if opt.resize_or_crop != 'none': # when training at full res this causes OOM
            torch.backends.cudnn.benchmark = True
        self.isTrain = opt.isTrain
        input_nc = opt.label_nc if opt.label_nc != 0 else opt.input_nc

        ##### define networks
        # Generator network
        netG_input_nc = input_nc + opt.output_nc
        if not opt.no_instance:
            netG_input_nc += 1

        self.netG = networks.define_G(netG_input_nc, opt.output_nc, opt.ngf, opt.netG,
                                      opt.n_downsample_global, opt.n_blocks_global, opt.n_local_enhancers,
                                      opt.n_blocks_local, opt.norm, gpu_ids=self.gpu_ids)

        # Discriminator network
        if self.isTrain:
            use_sigmoid = opt.no_lsgan
            netD_input_nc = 4*opt.output_nc
            #netD_input_nc = input_nc + opt.output_nc
            if not opt.no_instance:
                netD_input_nc += 1
            self.netD = networks.define_D(netD_input_nc, opt.ndf, opt.n_layers_D, opt.norm, use_sigmoid,
                                          opt.num_D, not opt.no_ganFeat_loss, gpu_ids=self.gpu_ids)

        # Face discriminator network
        if self.isTrain and opt.face:
            use_sigmoid = opt.no_lsgan
            netD_input_nc = 2*opt.output_nc
            if not opt.no_instance:
                netD_input_nc += 1
            self.netDface = networks.define_D_face(netD_input_nc, opt.ndf,
                    opt.n_layers_D, opt.norm, use_sigmoid, 1, not
                    opt.no_ganFeat_loss, gpu_ids=self.gpu_ids)

        #Face residual network
        if opt.face:
            self.faceGen = networks.define_G(opt.output_nc*2, opt.output_nc, 64, 'global',
                                  n_downsample_global=3, n_blocks_global=5, n_local_enhancers=0,
                                  n_blocks_local=0, norm=opt.norm, gpu_ids=self.gpu_ids)

        print('---------- Networks initialized -------------')

        # load networks
        if (not self.isTrain or opt.continue_train or opt.load_pretrain):
            pretrained_path = '' if not self.isTrain else opt.load_pretrain
            self.load_network(self.netG, 'G', opt.which_epoch, pretrained_path)
            if self.isTrain:
                self.load_network(self.netD, 'D', opt.which_epoch, pretrained_path)
                if opt.face:
                    self.load_network(self.netDface, 'Dface', opt.which_epoch, pretrained_path)
            if opt.face:
                self.load_network(self.faceGen, 'Gface', opt.which_epoch, pretrained_path)

        # set loss functions and optimizers
        if self.isTrain:
            if opt.pool_size > 0 and (len(self.gpu_ids)) > 1:
                raise NotImplementedError("Fake Pool Not Implemented for MultiGPU")
            self.fake_pool = ImagePool(opt.pool_size)
            self.old_lr = opt.lr

            # define loss functions
            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan, tensor=self.Tensor)
            self.criterionFeat = torch.nn.L1Loss()
            if not opt.no_vgg_loss:
                self.criterionVGG = networks.VGGLoss(self.gpu_ids)
            if opt.use_l1:
                self.criterionL1 = torch.nn.L1Loss()

            # Loss names
            self.loss_names = ['G_GAN', 'G_GAN_Feat', 'G_VGG', 'D_real', 'D_fake', 'G_GANface', 'D_realface', 'D_fakeface']

            # initialize optimizers
            # optimizer G
            if opt.niter_fix_global > 0:
                import sys
                if sys.version_info >= (3,0):
                    finetune_list = set()
                else:
                    from sets import Set
                    finetune_list = Set()

                params_dict = dict(self.netG.named_parameters())
                params = []
                for key, value in params_dict.items():       
                    if key.startswith('model' + str(opt.n_local_enhancers)):                    
                        params += [value]
                        finetune_list.add(key.split('.')[0])  
                print('------------- Only training the local enhancer network (for %d epochs) ------------' % opt.niter_fix_global)
                print('The layers that are finetuned are ', sorted(finetune_list))                         
            else:
                params = list(self.netG.parameters())

            if opt.face:
                params = list(self.faceGen.parameters())
            else:
                if opt.niter_fix_main == 0:
                    params += list(self.netG.parameters())

            self.optimizer_G = torch.optim.Adam(params, lr=opt.lr, betas=(opt.beta1, 0.999))

            # optimizer D
            if opt.niter > 0 and opt.face:
                print('------------- Only training the face discriminator network (for %d epochs) ------------' % opt.niter)
                params = list(self.netDface.parameters())
            else:
                if opt.face:
                    params = list(self.netD.parameters()) + list(self.netDface.parameters())
                else:
                    params = list(self.netD.parameters())

            self.optimizer_D = torch.optim.Adam(params, lr=opt.lr, betas=(opt.beta1, 0.999))

    def encode_label(self, label_map, infer=False):
        input_label = label_map.data.cuda()
        input_label = Variable(input_label, volatile=infer)
        return input_label

        if self.opt.label_nc == 0:
            input_label = label_map.data.cuda()
        else:
            # create one-hot vector for label map 
            size = label_map.size()
            oneHot_size = (size[0], self.opt.label_nc, size[2], size[3])
            input_label = torch.cuda.FloatTensor(torch.Size(oneHot_size)).zero_()
            input_label = input_label.scatter_(1, label_map.data.long().cuda(), 1.0)
            if self.opt.data_type == 16:
                input_label = input_label.half()

        # get edges from instance map
        if not self.opt.no_instance:
            inst_map = inst_map.data.cuda()
            edge_map = self.get_edges(inst_map)
            input_label = torch.cat((input_label, edge_map), dim=1)         

        input_label = Variable(input_label, volatile=infer)
        return input_label

    def encode_input(self, label_map, real_image=None, next_label=None, next_image=None, zeroshere=None, infer=False):

        input_label = self.encode_label(label_map, infer=infer)

        # next label for training
        if next_label is not None:
            next_label = self.encode_label(next_label, infer=infer)

        # real images for training
        if real_image is not None:
            real_image = Variable(real_image.data.cuda())

        # real images for training
        if next_image is not None:
            next_image = Variable(next_image.data.cuda())

        if zeroshere is not None:
            #zeroshere = self.encode_label(zeroshere, infer=infer)
            zeroshere = zeroshere.data.cuda()
            zeroshere = Variable(zeroshere, volatile=infer)

        return input_label, real_image, next_label, next_image, zeroshere

    def discriminate(self, input_label, test_image, use_pool=False):
        input_concat = torch.cat((input_label, test_image.detach()), dim=1)
        if use_pool:
            fake_query = self.fake_pool.query(input_concat)
            return self.netD.forward(fake_query)
        else:
            return self.netD.forward(input_concat)

    def discriminate_4(self, s0, s1, i0, i1, use_pool=False):
        input_concat = torch.cat((s0, s1, i0.detach(), i1.detach()), dim=1)
        if use_pool:
            fake_query = self.fake_pool.query(input_concat)
            return self.netD.forward(fake_query)
        else:
            return self.netD.forward(input_concat)

    def discriminateface(self, input_label, test_image, use_pool=False):
        input_concat = torch.cat((input_label, test_image.detach()), dim=1)
        if use_pool:
            fake_query = self.fake_pool.query(input_concat)
            return self.netDface.forward(fake_query)
        else:
            return self.netDface.forward(input_concat)


    def forward(self, label, next_label, image, next_image, face_coords, zeroshere, infer=False):
        input_label, real_image, next_label, next_image, zeroshere = self.encode_input(label, image,
                     next_label=next_label, next_image=next_image, zeroshere=zeroshere)

        if self.opt.face:
            minx = face_coords[0][0]
            miny = face_coords[0][1]
            maxx = face_coords[0][2]
            maxy = face_coords[0][3]

        initial_I_0 = 0

        # Fake Generation I_0
        input_concat = torch.cat((input_label, zeroshere), dim=1)

        #face residual for I_0
        face_residual_0 = 0
        if self.opt.face:
            initial_I_0 = self.netG.forward(input_concat)
            face_label_0 = input_label[:, :, miny:maxy, minx:maxx]
            face_residual_0 = self.faceGen.forward(torch.cat((face_label_0, initial_I_0[:, :, miny:maxy, minx:maxx]), dim=1))
            I_0 = initial_I_0.clone()
            I_0[:, :, miny:maxy, minx:maxx] = initial_I_0[:, :, miny:maxy, minx:maxx] + face_residual_0
        else:
            I_0 = self.netG.forward(input_concat)


        input_concat1 = torch.cat((next_label, I_0), dim=1)

        #face residual for I_1
        face_residual_1 = 0
        if self.opt.face:
            initial_I_1 = self.netG.forward(input_concat1)
            face_label_1 = next_label[:, :, miny:maxy, minx:maxx]
            face_residual_1 = self.faceGen.forward(torch.cat((face_label_1, initial_I_1[:, :, miny:maxy, minx:maxx]), dim=1))
            I_1 = initial_I_1.clone()
            I_1[:, :, miny:maxy, minx:maxx] = initial_I_1[:, :, miny:maxy, minx:maxx] + face_residual_1
        else:
            I_1 = self.netG.forward(input_concat1)

        loss_D_fake_face = loss_D_real_face = loss_G_GAN_face = 0
        fake_face_0 = fake_face_1 = real_face_0 = real_face_1 = 0
        fake_face = real_face = face_residual = 0
        if self.opt.face:

            fake_face_0 = I_0[:, :, miny:maxy, minx:maxx]
            fake_face_1 = I_1[:, :, miny:maxy, minx:maxx]
            real_face_0 = real_image[:, :, miny:maxy, minx:maxx]
            real_face_1 = next_image[:, :, miny:maxy, minx:maxx]

            # Fake Detection and Loss
            pred_fake_pool_face = self.discriminateface(face_label_0, fake_face_0, use_pool=True)
            loss_D_fake_face += 0.5 * self.criterionGAN(pred_fake_pool_face, False)

            # Face Real Detection and Loss
            pred_real_face = self.discriminateface(face_label_0, real_face_0)
            loss_D_real_face += 0.5 * self.criterionGAN(pred_real_face, True)

            # Face GAN loss (Fake Passability Loss)
            pred_fake_face = self.netDface.forward(torch.cat((face_label_0, fake_face_0), dim=1))
            loss_G_GAN_face += 0.5 * self.criterionGAN(pred_fake_face, True)

            pred_fake_pool_face = self.discriminateface(face_label_1, fake_face_1, use_pool=True)
            loss_D_fake_face += 0.5 * self.criterionGAN(pred_fake_pool_face, False)

            # Face Real Detection and Loss
            pred_real_face = self.discriminateface(face_label_1, real_face_1)
            loss_D_real_face += 0.5 * self.criterionGAN(pred_real_face, True)

            # Face GAN loss (Fake Passability Loss)
            pred_fake_face = self.netDface.forward(torch.cat((face_label_1, fake_face_1), dim=1))
            loss_G_GAN_face += 0.5 * self.criterionGAN(pred_fake_face, True)

            fake_face = torch.cat((fake_face_0, fake_face_1), dim=3)
            real_face = torch.cat((real_face_0, real_face_1), dim=3)

            face_residual = torch.cat((face_residual_0, face_residual_1), dim=3)

        # Fake Detection and Loss
        pred_fake_pool = self.discriminate_4(input_label, next_label, I_0, I_1, use_pool=True)
        loss_D_fake = self.criterionGAN(pred_fake_pool, False)

        # Real Detection and Loss
        pred_real = self.discriminate_4(input_label, next_label, real_image, next_image)
        loss_D_real = self.criterionGAN(pred_real, True)

        # GAN loss (Fake Passability Loss)
        pred_fake = self.netD.forward(torch.cat((input_label, next_label, I_0, I_1), dim=1))
        loss_G_GAN = self.criterionGAN(pred_fake, True)

        # GAN feature matching loss
        loss_G_GAN_Feat = 0
        if not self.opt.no_ganFeat_loss:
            feat_weights = 4.0 / (self.opt.n_layers_D + 1)
            D_weights = 1.0 / self.opt.num_D
            for i in range(self.opt.num_D):
                for j in range(len(pred_fake[i])-1):
                    loss_G_GAN_Feat += D_weights * feat_weights * \
                        self.criterionFeat(pred_fake[i][j], pred_real[i][j].detach()) * self.opt.lambda_feat

        # VGG feature matching loss
        loss_G_VGG = 0
        if not self.opt.no_vgg_loss:
            loss_G_VGG0 = self.criterionVGG(I_0, real_image) * self.opt.lambda_feat
            loss_G_VGG1 = self.criterionVGG(I_1, next_image) * self.opt.lambda_feat
            loss_G_VGG = loss_G_VGG0 + loss_G_VGG1
            if self.opt.netG == 'global': #need 2x VGG for artifacts when training local
                loss_G_VGG *= 0.5
            if self.opt.face:
                loss_G_VGG += 0.5 * self.criterionVGG(fake_face_0, real_face_0) * self.opt.lambda_feat
                loss_G_VGG += 0.5 * self.criterionVGG(fake_face_1, real_face_1) * self.opt.lambda_feat

        if self.opt.use_l1:
            loss_G_VGG += (self.criterionL1(I_1, next_image)) * self.opt.lambda_A
        #gpu_profile(frame=sys._getframe(), event='line', arg=None)

        # Only return the fake_B image if necessary to save BW
        return [ [ loss_G_GAN, loss_G_GAN_Feat, loss_G_VGG, loss_D_real, loss_D_fake,
                    loss_G_GAN_face, loss_D_real_face,  loss_D_fake_face],
                        None if not infer else [torch.cat((I_0, I_1), dim=3), fake_face, face_residual, initial_I_0] ]


    def get_edges(self, t):
        edge = torch.cuda.ByteTensor(t.size()).zero_()
        edge[:,:,:,1:] = edge[:,:,:,1:] | (t[:,:,:,1:] != t[:,:,:,:-1])
        edge[:,:,:,:-1] = edge[:,:,:,:-1] | (t[:,:,:,1:] != t[:,:,:,:-1])
        edge[:,:,1:,:] = edge[:,:,1:,:] | (t[:,:,1:,:] != t[:,:,:-1,:])
        edge[:,:,:-1,:] = edge[:,:,:-1,:] | (t[:,:,1:,:] != t[:,:,:-1,:])
        if self.opt.data_type == 16:
            return edge.half()
        else:
            return edge.float()

    def save(self, which_epoch):
        self.save_network(self.netG, 'G', which_epoch, self.gpu_ids)
        self.save_network(self.netD, 'D', which_epoch, self.gpu_ids)
        if self.opt.face:
            self.save_network(self.netDface, 'Dface', which_epoch, self.gpu_ids)
            self.save_network(self.faceGen, 'Gface', which_epoch, self.gpu_ids)

    def update_fixed_params(self):
        # after fixing the global generator for a number of iterations, also start finetuning it
        params = list(self.netG.parameters())
        if self.opt.face:
            params += list(self.faceGen.parameters())
        self.optimizer_G = torch.optim.Adam(params, lr=self.opt.lr, betas=(self.opt.beta1, 0.999))
        print('------------ Now also finetuning global generator -----------')

    def update_fixed_params_netD(self):
        params = list(self.netD.parameters()) + list(self.netDface.parameters())
        self.optimizer_D = torch.optim.Adam(params, lr=self.opt.lr, betas=(self.opt.beta1, 0.999))
        print('------------ Now also finetuning multiscale discriminator -----------')

    def update_learning_rate(self):
        lrd = self.opt.lr / self.opt.niter_decay
        lr = self.old_lr - lrd
        for param_group in self.optimizer_D.param_groups:
            param_group['lr'] = lr
        for param_group in self.optimizer_G.param_groups:
            param_group['lr'] = lr
        print('update learning rate: %f -> %f' % (self.old_lr, lr))
        self.old_lr = lr

    def inference(self, label, prevouts, face_coords):

        # Encode Inputs        
        input_label, _, _, _, prevouts = self.encode_input(Variable(label), zeroshere=Variable(prevouts), infer=True)

        if self.opt.face:
            minx = face_coords[0][0]
            miny = face_coords[0][1]
            maxx = face_coords[0][2]
            maxy = face_coords[0][3]

        """ new face """
        I_0 = 0
        # Fake Generation

        input_concat = torch.cat((input_label, prevouts), dim=1) 
        initial_I_0 = self.netG.forward(input_concat)

        if self.opt.face:
            face_label_0 = input_label[:, :, miny:maxy, minx:maxx]
            face_residual_0 = self.faceGen.forward(torch.cat((face_label_0, initial_I_0[:, :, miny:maxy, minx:maxx]), dim=1))
            I_0 = initial_I_0.clone()
            I_0[:, :, miny:maxy, minx:maxx] = initial_I_0[:, :, miny:maxy, minx:maxx] + face_residual_0
            fake_face_0 = I_0[:, :, miny:maxy, minx:maxx]
            return I_0
        return initial_I_0
