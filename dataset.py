from __future__ import print_function, division
import os
import torch
import re
import xml.etree.ElementTree as ET
import numpy as np
from skimage import io
from skimage.transform import resize
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, utils
from PIL import Image, ImageDraw
from utils import *

class ToothImageDataset(Dataset):
    """Dataset of dental panoramic x-rays"""

    INPUT_SIZE = (1600, 800)
    OUTPUT_SIZE = (50, 25)
    OUTPUT_CELL_SIZE = float(INPUT_SIZE[0]) / float(OUTPUT_SIZE[0])

    # # constants about receptive field for anchors
    # # precalculated here https://github.com/tensorflow/tensorflow/tree/master/tensorflow/contrib/receptive_field
    RECEPTIVE_FIELD = 212

    # anchors constants
    ANCHORS_WIDTH_RATIOS = [0.3, 0.5, 1.0]
    ANCHORS_HEIGHT_RATIOS = [0.3, 0.5, 1.0]

    NUMBER_ANCHORS_WIDE = OUTPUT_SIZE[0]
    NUMBER_ANCHORS_HEIGHT = OUTPUT_SIZE[1]

    NEGATIVE_THRESHOLD = 0.3
    POSITIVE_THRESHOLD = 0.5

    ANCHOR_SAMPLING_SIZE = 256

    def __init__(self, root_dir):
        """
        Args:
            root_dir (string): Directory with all the images under VOC format.
        """
        self.root_dir = root_dir
        self.label_map_path = os.path.join(root_dir, 'pascal_label_map.pbtxt')
        self.tooth_images_paths = os.listdir(os.path.join(root_dir, 'Annotations'))
        self.label_map = self.get_label_map(self.label_map_path)
        self.inverse_label_map = self.get_inverse_label_map(self.label_map_path)

        self.anchor_dimensions = self.get_anchor_dimensions()
        self.anchor_number = len(self.anchor_dimensions)

    def __len__(self):
        return len(self.tooth_images_paths)

    def __getitem__(self, i):
        image = self.get_image(i)
        bboxes = self.get_truth_bboxes(i)
        anchors = self.get_image_anchors()
        im = np.expand_dims(np.stack((resize(image, self.INPUT_SIZE),)*3), axis=0)
        truth_bbox, positives, negatives = self.get_positive_negative_anchors(anchors, bboxes)
        reg_target = self.parametrize(anchors, truth_bbox)

        indices = np.array([i for i in range(len(anchors.reshape((-1, 4))))])
        selected_indices, positive_indices = self.get_selected_indices_sample(indices, positives, negatives)
        return torch.from_numpy(im), torch.from_numpy(reg_target.reshape((-1, 4))), torch.from_numpy(positives.astype(int).reshape((-1, 2))), selected_indices, positive_indices

    def get_anchor_dimensions(self):
        dimensions = []
        for w in self.ANCHORS_WIDTH_RATIOS:
            for h in self.ANCHORS_HEIGHT_RATIOS:
                dimensions.append((w, h))
        return dimensions

    def get_image(self, i):
        path = os.path.join(self.root_dir, 'JPEGImages', str(i) + '.png')
        return io.imread(path)

    def get_selected_indices_sample(self, indices, positives, negatives):
        positive_indices = indices[positives.reshape(-1)]
        negative_indices = indices[negatives.reshape(-1)]
        random_positives = np.random.permutation(positive_indices)[:self.ANCHOR_SAMPLING_SIZE // 2]
        random_negatives = np.random.permutation(negative_indices)[:self.ANCHOR_SAMPLING_SIZE - len(random_positives)]
        selected_indices = np.concatenate((random_positives, random_negatives))
        return selected_indices, positive_indices

    def get_anchors_at_position(self, pos):
        """
        position (x, y)
        """
        # returns something (self.anchor_number, 4)
        # each anchor is (x, y, w, h)
        x, y = pos
        anchors = np.zeros((self.anchor_number, 4))
        for i in range(self.anchor_number):
            center_x = self.OUTPUT_CELL_SIZE * (float(x) + 0.5)
            center_y = self.OUTPUT_CELL_SIZE * (float(y) + 0.5)

            width = self.anchor_dimensions[i][0] * self.RECEPTIVE_FIELD
            height = self.anchor_dimensions[i][1] * self.RECEPTIVE_FIELD

            top_x = center_x - width / 2.0
            top_y = center_y - height / 2.0
            anchors[i, :] = [top_x, top_y, top_x + width, top_y + height]
        return anchors

    def get_image_anchors(self):
        anchors = np.zeros((self.NUMBER_ANCHORS_WIDE, self.NUMBER_ANCHORS_HEIGHT, self.anchor_number, 4))

        for i in range(self.NUMBER_ANCHORS_WIDE):
            for j in range(self.NUMBER_ANCHORS_HEIGHT):
                anchors_pos = self.get_anchors_at_position((i, j))
                anchors[i, j, :] = anchors_pos

        return anchors

    def get_truth_bboxes(self, i):
        path = os.path.join(self.root_dir, 'Annotations', str(i) + '.xml')
        tree = ET.parse(path)
        root = tree.getroot()

        # we need to resize the bboxes according to the INPUT_SIZE
        size = root.find('size')
        height = int(size.find('height').text)
        width = int(size.find('width').text)
        width_ratio = float(width) / float(self.INPUT_SIZE[0])
        height_ratio = float(height) / float(self.INPUT_SIZE[1])

        raw_boxes = [child for child in root if child.tag == 'object']
        classes = [self.inverse_label_map[c[0].text] for c in raw_boxes]
        # TODO be sure that the order is always the same (xmin, ymin, xmax, ymax)
        bboxes = np.array([[[int(d.text) for d in c] for c in object if c.tag == 'bndbox'] for object in raw_boxes])
        if not len(bboxes):
            return np.array([])

        bboxes = bboxes.reshape(-1, bboxes.shape[-1])
        for i in [0, 2]:
            bboxes[:, i] = bboxes[:, i] / width_ratio
        for i in [1, 3]:
            bboxes[:, i] = bboxes[:, i] / height_ratio
        return bboxes

    def get_positive_negative_anchors(self, anchors, bboxes):
        if not len(bboxes):
            ious = np.zeros((self.NUMBER_ANCHORS_WIDE, self.NUMBER_ANCHORS_HEIGHT, self.anchor_number))
            positives = ious > self.POSITIVE_THRESHOLD
            negatives = ious < self.NEGATIVE_THRESHOLD
            return np.array([]), positives, negatives

        ious = np.zeros((self.NUMBER_ANCHORS_WIDE, self.NUMBER_ANCHORS_HEIGHT, self.anchor_number, len(bboxes)))
        for i in range(anchors.shape[0]):
            for j in range(anchors.shape[1]):
                for n in range(anchors.shape[2]):
                    for b in range(len(bboxes)):
                        ious[i, j, n, b] = IoU(anchors[i, j, n], bboxes[b])
        arg_truth_bbox = np.argmax(ious, axis=3).flatten()
        truth_bbox = bboxes[arg_truth_bbox, :].reshape(ious.shape[:3] + (bboxes.shape[-1],))

        max_iou_per_anchor = np.amax(ious, axis=3)
        positives = max_iou_per_anchor > self.POSITIVE_THRESHOLD
        negatives = max_iou_per_anchor < self.NEGATIVE_THRESHOLD
        return truth_bbox, positives, negatives

    def get_label_map(self, label_map_path):
        return get_label_map_from_pbtxt(label_map_path)

    def get_inverse_label_map(self, label_map_path):
        return get_inverse_label_map_from_pbtxt(label_map_path)

    def parametrize(self, anchors, bboxes):
        reg = np.zeros(anchors.shape, dtype = np.float32)
        if not len(bboxes):
            return reg

        reg[:, :, :, 0] = (bboxes[:, :, :, 0] - anchors[:, :, :, 0]) / (anchors[:, :, :, 2] - anchors[:, :, :, 0])
        reg[:, :, :, 1] = (bboxes[:, :, :, 1] - anchors[:, :, :, 1]) / (anchors[:, :, :, 3] - anchors[:, :, :, 1])
        reg[:, :, :, 2] = np.log((bboxes[:, :, :, 2] - bboxes[:, :, :, 0]) / (anchors[:, :, :, 2] - anchors[:, :, :, 0]) )
        reg[:, :, :, 3] = np.log((bboxes[:, :, :, 3] - bboxes[:, :, :, 1]) / (anchors[:, :, :, 3] - anchors[:, :, :, 1]) )

        return np.nan_to_num(reg)

    def visualise_anchors_on_image(self, i):
        image = self.get_image(i)
        temp_im = Image.fromarray(image).resize(self.INPUT_SIZE)
        im = Image.new("RGBA", temp_im.size)
        im.paste(temp_im)

        draw = ImageDraw.Draw(im)

        bboxes = self.get_truth_bboxes(i)
        for bbox in bboxes:
            draw.rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], outline = 'blue')

        positives, negatives, truth_bbox = self.get_positive_negative_anchors(self.get_image_anchors(), bboxes)
        for bbox in positives:
            draw.rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], outline = 'green')

        im.show()
