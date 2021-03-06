#!/usr/bin/env python
import os
import sys
from tqdm import tqdm
from PIL import Image
import torch
import shutil

from motion_transfer.paths import build_paths
from motion_transfer.video_options import VideoOptions
from motion_transfer.video_utils import video_from_frame_directory, video_filename_for_codec, Codec

from motion_transfer.data_loader import CreateDataLoader
from motion_transfer.models import create_model
# pix2pixHD imports
sys.path.append('./vendor/pix2pixHD/')
import util.util as util



args = VideoOptions()
args = args.parse(save=False)
args.nThreads = 1   # test code only supports nThreads = 1
args.batchSize = 1  # test code only supports batchSize = 1
args.serial_batches = True  # no shuffle
args.no_flip = True  # no flip
args.resize_or_crop = "none"
name = args.name if args.results_name is None else args.results_name

paths = build_paths(args)

codec = Codec[args.codec]

data_loader = CreateDataLoader(args)
dataset = data_loader.load_data()


nframes = args.how_many if args.how_many is not None else len(dataset)
duration_s = nframes / args.fps
video_id = "epoch-%s_%s_%ds_%dfps%s" % (
    str(args.which_epoch),
    name,
    duration_s,
    args.fps,
    args.output_suffix
)

frame_dir = paths.results_dir / video_id
video_path = video_filename_for_codec(paths.results_dir / video_id, codec)
frame_dir.mkdir(parents=True, exist_ok=True)

model = create_model(args)

prev = None

for i, data in enumerate(tqdm(dataset)):
    fn = frame_dir / ("frame-%s.png" % str(i + 1).zfill(6))
    if fn.exists(): continue

    if args.how_many is not None and i >= args.how_many:
        break

    if prev is None:
        prev = torch.zeros(data['label'].size())

    if args.data_type == 16:
        data['label'] = data['label'].half()
        data['inst']  = data['inst'].half()
        prev = prev.half()
    elif args.data_type == 8:
        data['label'] = data['label'].uint8()
        data['inst']  = data['inst'].uint8()
        prev = prev.uint8()

    with torch.no_grad():
        inferred = model.inference(data['label'], prev, data['face_coords'])
    img_nda = util.tensor2im(inferred.data[0])
    img_pil = Image.fromarray(img_nda)
    img_pil.save(fn)

    prev = inferred.data

    del inferred
    del img_nda
    del img_pil



if not video_path.exists():
    video_from_frame_directory(
        frame_dir, 
        video_path, 
        framerate=args.fps,
        codec=codec
    )

print("video ready:\n%s" % video_path)
