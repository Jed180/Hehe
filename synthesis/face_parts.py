#!/usr/bin/env python

import argparse
import cv2 as cv
import numpy as np
from tqdm import tqdm
from pathlib import Path
import os
if __name__ == '__main__':
    import sys
    sys.path.append(os.getcwd())
from motion_transfer.paths import build_paths, data_paths_for_idx, create_directories
from motion_transfer.labelling import Labeller
from motion_transfer.pose import Pose

def parse_arguments():
    p = argparse.ArgumentParser(description="Generate labels based on programmatic movement", fromfile_prefix_chars='@')
    p.add_argument('--dataroot', type=str)
    p.add_argument('-n', '--nframes', help='Number of frames to generate', required=True, type=int)
    p.add_argument('--frame-offset', help='The frame offset for the two datasets', required=True, type=int)
    p.add_argument('--target-label-offset', required=True, type=int)
    p = Labeller.add_arguments(p)

    return p.parse_args()

print("Synthesizing face puppet")

args = parse_arguments()
paths_base = build_paths(args, directory_prefix='base')
paths_out = build_paths(args, directory_prefix='test')
create_directories(paths_out)
labeller = Labeller.build_from_arguments(args, paths_base)
labeller_t = Labeller.build_from_arguments(args, paths_base, label_offset=args.target_label_offset)

del labeller.face_labeller.landmarks["right_eyebrow"]
del labeller.face_labeller.landmarks["left_eyebrow"]
del labeller.face_labeller.landmarks["right_eye"]
del labeller.face_labeller.landmarks["left_eye"]

del labeller_t.face_labeller.landmarks["mouth"]
del labeller_t.face_labeller.landmarks["inner_mouth"]
del labeller_t.face_labeller.landmarks["nose"]
del labeller_t.face_labeller.landmarks["jaw"]

base_image_fns = sorted(os.listdir(paths_base.img_dir))

for i in tqdm(range(0,args.nframes)):
    t = float(i) / float(args.nframes)
    paths = data_paths_for_idx(paths_out, i)

    if paths.label.exists():
        continue

    path_base_image = paths_base.img_dir / base_image_fns[i]

    base_image = cv.imread(str(path_base_image))
    if base_image is None:
        raise Exception("could not read image: {}".format(path_base_image))

    faces = labeller.face_labeller.detect(base_image)
    pose = Pose(labeller.detect_pose(base_image))

    labels = np.zeros(base_image.shape, dtype=np.uint8)
    labels_t = np.zeros(base_image.shape, dtype=np.uint8)
    center = (labels.shape[1] / 2, labels.shape[0] / 2)

    for face in faces:
        fcenter = np.mean(face.nose, axis=0)

        offset = center - fcenter

        for (name, shape) in face.shapes():
            face[name] = shape + offset

        labels = labeller.face_labeller.visualize_facial_landmarks(labels, face.shape, alpha=1.0)
        labels = labeller_t.face_labeller.visualize_facial_landmarks(labels, face.shape, alpha=1.0)

    points = np.where(pose.points >= 0, pose.points + offset, pose.points)
    labels = labeller.draw_labels(labels, points)

    cv.imwrite(str(paths.label), labels)
