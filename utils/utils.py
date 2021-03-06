from __future__ import division
import math
import time
import tqdm
import os
import re
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import random
from matplotlib.ticker import NullLocator
from PIL import Image
import copy
import cv2



def to_cpu(tensor):
    return tensor.detach().cpu()


def load_classes(path):
    """
    Loads class labels at 'path'
    """
    fp = open(path, "r")
    names = fp.read().split("\n")[:-1]
    return names


def weights_init_normal(m):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        torch.nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif classname.find("BatchNorm2d") != -1:
        torch.nn.init.normal_(m.weight.data, 1.0, 0.02)
        torch.nn.init.constant_(m.bias.data, 0.0)


def rescale_boxes(boxes, current_dim, original_shape):
    """ Rescales bounding boxes to the original shape """
    orig_h, orig_w = original_shape
    # The amount of padding that was added
    pad_x = max(orig_h - orig_w, 0) * (current_dim / max(original_shape))
    pad_y = max(orig_w - orig_h, 0) * (current_dim / max(original_shape))
    # Image height and width after padding is removed
    unpad_h = current_dim - pad_y
    unpad_w = current_dim - pad_x
    # Rescale bounding boxes to dimension of original image
    boxes[:, 0] = ((boxes[:, 0] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 1] = ((boxes[:, 1] - pad_y // 2) / unpad_h) * orig_h
    boxes[:, 2] = ((boxes[:, 2] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 3] = ((boxes[:, 3] - pad_y // 2) / unpad_h) * orig_h
    return boxes


def xywh2xyxy(x):
    y = x.new(x.shape)
    y[..., 0] = x[..., 0] - x[..., 2] / 2
    y[..., 1] = x[..., 1] - x[..., 3] / 2
    y[..., 2] = x[..., 0] + x[..., 2] / 2
    y[..., 3] = x[..., 1] + x[..., 3] / 2
    return y


def ap_per_class(tp, conf, pred_cls, target_cls):
    """ Compute the average precision, given the recall and precision curves.
    Source: https://github.com/rafaelpadilla/Object-Detection-Metrics.
    # Arguments
        tp:    True positives (list).
        conf:  Objectness value from 0-1 (list).
        pred_cls: Predicted object classes (list).
        target_cls: True object classes (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """

    # Sort by objectness
    i = np.argsort(-conf)
    tp, conf, pred_cls = tp[i], conf[i], pred_cls[i]

    # Find unique classes
    unique_classes = np.unique(target_cls)

    # Create Precision-Recall curve and compute AP for each class
    ap, p, r = [], [], []
    for c in tqdm.tqdm(unique_classes, desc="Computing AP"):
        i = pred_cls == c
        n_gt = (target_cls == c).sum()  # Number of ground truth objects
        n_p = i.sum()  # Number of predicted objects

        if n_p == 0 and n_gt == 0:
            continue
        elif n_p == 0 or n_gt == 0:
            ap.append(0)
            r.append(0)
            p.append(0)
        else:
            # Accumulate FPs and TPs
            fpc = (1 - tp[i]).cumsum()
            tpc = (tp[i]).cumsum()

            # Recall
            recall_curve = tpc / (n_gt + 1e-16)
            r.append(recall_curve[-1])

            # Precision
            precision_curve = tpc / (tpc + fpc)
            p.append(precision_curve[-1])

            # AP from recall-precision curve
            ap.append(compute_ap(recall_curve, precision_curve))

    # Compute F1 score (harmonic mean of precision and recall)
    p, r, ap = np.array(p), np.array(r), np.array(ap)
    f1 = 2 * p * r / (p + r + 1e-16)

    return p, r, ap, f1, unique_classes.astype("int32")


def compute_ap(recall, precision):
    """ Compute the average precision, given the recall and precision curves.
    Code originally from https://github.com/rbgirshick/py-faster-rcnn.

    # Arguments
        recall:    The recall curve (list).
        precision: The precision curve (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """
    # correct AP calculation
    # first append sentinel values at the end
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.concatenate(([0.0], precision, [0.0]))

    # compute the precision envelope
    for i in range(mpre.size - 1, 0, -1):
        mpre[i - 1] = np.maximum(mpre[i - 1], mpre[i])

    # to calculate area under PR curve, look for points
    # where X axis (recall) changes value
    i = np.where(mrec[1:] != mrec[:-1])[0]

    # and sum (\Delta recall) * prec
    ap = np.sum((mrec[i + 1] - mrec[i]) * mpre[i + 1])
    return ap


def get_batch_statistics(outputs, targets, iou_threshold):
    """ Compute true positives, predicted scores and predicted labels per sample """
    batch_metrics = []
    for sample_i in range(len(outputs)):

        if outputs[sample_i] is None:
            continue

        output = outputs[sample_i]
        pred_boxes = output[:, :4]
        pred_scores = output[:, 4]
        pred_labels = output[:, -1]

        true_positives = np.zeros(pred_boxes.shape[0])

        annotations = targets[targets[:, 0] == sample_i][:, 1:]
        target_labels = annotations[:, 0] if len(annotations) else []
        if len(annotations):
            detected_boxes = []
            target_boxes = annotations[:, 1:]

            for pred_i, (pred_box, pred_label) in enumerate(zip(pred_boxes, pred_labels)):

                # If targets are found break
                if len(detected_boxes) == len(annotations):
                    break

                # Ignore if label is not one of the target labels
                if pred_label not in target_labels:
                    continue

                iou, box_index = bbox_iou(pred_box.unsqueeze(0), target_boxes).max(0)
                if iou >= iou_threshold and box_index not in detected_boxes:
                    true_positives[pred_i] = 1
                    detected_boxes += [box_index]
        batch_metrics.append([true_positives, pred_scores, pred_labels])
    return batch_metrics


def bbox_wh_iou(wh1, wh2):
    wh2 = wh2.t()
    w1, h1 = wh1[0], wh1[1]
    w2, h2 = wh2[0], wh2[1]
    inter_area = torch.min(w1, w2) * torch.min(h1, h2)
    union_area = (w1 * h1 + 1e-16) + w2 * h2 - inter_area
    return inter_area / union_area


def bbox_iou(box1, box2, x1y1x2y2=True):
    """
    Returns the IoU of two bounding boxes
    """
    if not x1y1x2y2:
        # Transform from center and width to exact coordinates
        b1_x1, b1_x2 = box1[:, 0] - box1[:, 2] / 2, box1[:, 0] + box1[:, 2] / 2
        b1_y1, b1_y2 = box1[:, 1] - box1[:, 3] / 2, box1[:, 1] + box1[:, 3] / 2
        b2_x1, b2_x2 = box2[:, 0] - box2[:, 2] / 2, box2[:, 0] + box2[:, 2] / 2
        b2_y1, b2_y2 = box2[:, 1] - box2[:, 3] / 2, box2[:, 1] + box2[:, 3] / 2
    else:
        # Get the coordinates of bounding boxes
        b1_x1, b1_y1, b1_x2, b1_y2 = box1[:, 0], box1[:, 1], box1[:, 2], box1[:, 3]
        b2_x1, b2_y1, b2_x2, b2_y2 = box2[:, 0], box2[:, 1], box2[:, 2], box2[:, 3]

    # get the corrdinates of the intersection rectangle
    inter_rect_x1 = torch.max(b1_x1, b2_x1)
    inter_rect_y1 = torch.max(b1_y1, b2_y1)
    inter_rect_x2 = torch.min(b1_x2, b2_x2)
    inter_rect_y2 = torch.min(b1_y2, b2_y2)
    # Intersection area
    inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 + 1, min=0) * torch.clamp(
        inter_rect_y2 - inter_rect_y1 + 1, min=0
    )
    # Union Area
    b1_area = (b1_x2 - b1_x1 + 1) * (b1_y2 - b1_y1 + 1)
    b2_area = (b2_x2 - b2_x1 + 1) * (b2_y2 - b2_y1 + 1)

    iou = inter_area / (b1_area + b2_area - inter_area + 1e-16)

    return iou


def non_max_suppression(prediction, conf_thres=0.5, nms_thres=0.4):
    """
    Removes detections with lower object confidence score than 'conf_thres' and performs
    Non-Maximum Suppression to further filter detections.
    Returns detections with shape:
        (x1, y1, x2, y2, object_conf, class_score, class_pred)
    """

    # From (center x, center y, width, height) to (x1, y1, x2, y2)
    prediction[..., :4] = xywh2xyxy(prediction[..., :4])
    output = [None for _ in range(len(prediction))]
    for image_i, image_pred in enumerate(prediction):
        # Filter out confidence scores below threshold
        image_pred = image_pred[image_pred[:, 4] >= conf_thres]
        # If none are remaining => process next image
        if not image_pred.size(0):
            continue
        # Object confidence times class confidence
        score = image_pred[:, 4] * image_pred[:, 5:].max(1)[0]
        # Sort by it
        image_pred = image_pred[(-score).argsort()]
        class_confs, class_preds = image_pred[:, 5:].max(1, keepdim=True)
        detections = torch.cat((image_pred[:, :5], class_confs.float(), class_preds.float()), 1)
        # Perform non-maximum suppression
        keep_boxes = []
        while detections.size(0):
            large_overlap = bbox_iou(detections[0, :4].unsqueeze(0), detections[:, :4]) > nms_thres
            label_match = detections[0, -1] == detections[:, -1]
            # Indices of boxes with lower confidence scores, large IOUs and matching labels
            invalid = large_overlap & label_match
            weights = detections[invalid, 4:5]
            # Merge overlapping bboxes by order of confidence
            detections[0, :4] = (weights * detections[invalid, :4]).sum(0) / weights.sum()
            keep_boxes += [detections[0]]
            detections = detections[~invalid]
        if keep_boxes:
            output[image_i] = torch.stack(keep_boxes)

    return output


def build_targets(pred_boxes, pred_cls, target, anchors, ignore_thres):

    ByteTensor = torch.cuda.ByteTensor if pred_boxes.is_cuda else torch.ByteTensor
    FloatTensor = torch.cuda.FloatTensor if pred_boxes.is_cuda else torch.FloatTensor

    nB = pred_boxes.size(0)
    nA = pred_boxes.size(1)
    nC = pred_cls.size(-1)
    nG = pred_boxes.size(2)

    # Output tensors
    obj_mask = ByteTensor(nB, nA, nG, nG).fill_(0)
    noobj_mask = ByteTensor(nB, nA, nG, nG).fill_(1)
    class_mask = FloatTensor(nB, nA, nG, nG).fill_(0)
    iou_scores = FloatTensor(nB, nA, nG, nG).fill_(0)
    tx = FloatTensor(nB, nA, nG, nG).fill_(0)
    ty = FloatTensor(nB, nA, nG, nG).fill_(0)
    tw = FloatTensor(nB, nA, nG, nG).fill_(0)
    th = FloatTensor(nB, nA, nG, nG).fill_(0)
    tcls = FloatTensor(nB, nA, nG, nG, nC).fill_(0)

    # Convert to position relative to box
    target_boxes = target[:, 2:6] * nG
    gxy = target_boxes[:, :2]
    gwh = target_boxes[:, 2:]

    # Get anchors with best iou
    ious = torch.stack([bbox_wh_iou(anchor, gwh) for anchor in anchors])
    best_ious, best_n = ious.max(0)

    # Separate target values
    b, target_labels = target[:, :2].long().t()
    gx, gy = gxy.t()
    gw, gh = gwh.t()
    gi, gj = gxy.long().t()
    
    # Set masks
    obj_mask[b, best_n, gj, gi] = 1
    noobj_mask[b, best_n, gj, gi] = 0

    # Set noobj mask to zero where iou exceeds ignore threshold
    for i, anchor_ious in enumerate(ious.t()):
        noobj_mask[b[i], anchor_ious > ignore_thres, gj[i], gi[i]] = 0

    # Coordinates
    tx[b, best_n, gj, gi] = gx - gx.floor()
    ty[b, best_n, gj, gi] = gy - gy.floor()
    
    # Width and height
    tw[b, best_n, gj, gi] = torch.log(gw / anchors[best_n][:, 0] + 1e-16)
    th[b, best_n, gj, gi] = torch.log(gh / anchors[best_n][:, 1] + 1e-16)
    
    # One-hot encoding of label
    tcls[b, best_n, gj, gi, target_labels] = 1
    
    # Compute label correctness and iou at best anchor
    class_mask[b, best_n, gj, gi] = (pred_cls[b, best_n, gj, gi].argmax(-1) == target_labels).float()
    iou_scores[b, best_n, gj, gi] = bbox_iou(pred_boxes[b, best_n, gj, gi], target_boxes, x1y1x2y2=False)

    tconf = obj_mask.float()
    return iou_scores, class_mask, obj_mask, noobj_mask, tx, ty, tw, th, tcls, tconf

def plot_rescaled_boxes_on_image(img, bboxes, classes, model_input_size, verbose=0, **kwargs):

    # Create plot
    fig, ax = plt.subplots(1, figsize=kwargs.get("figsize", (8,8)))
    ax.imshow(img)

    # Create Colors
    cmap = plt.get_cmap("tab20b")
    colors = [cmap(i) for i in np.linspace(0, 1, 20)]

    # Draw bounding boxes and labels of detections
    if bboxes is not None:

        # Rescale boxes to original image
        if not (model_input_size, model_input_size) == img.shape[:2]:
            detections = rescale_boxes(bboxes, model_input_size, img.shape[:2])
        else:
            detections = bboxes
        unique_labels = detections[:, -1].cpu().unique()
        n_cls_preds = len(unique_labels)
        bbox_colors = random.sample(colors, n_cls_preds)
        for x1, y1, x2, y2, conf, cls_conf, cls_pred in detections:

            if verbose:
                print("\t+ Label: %s, Conf: %.5f" % (classes[int(cls_pred)], cls_conf.item()))

            box_w = x2 - x1
            box_h = y2 - y1

            color = bbox_colors[int(np.where(unique_labels == int(cls_pred))[0])]

            # Create a Rectangle patch
            bbox = patches.Rectangle((x1, y1), box_w, box_h, linewidth=2, edgecolor=color, facecolor="none")

            # Add the bbox to the plot
            ax.add_patch(bbox)

            # Add label
            plt.text(
                x1,
                y1,
                s=classes[int(cls_pred)],
                color="white",
                verticalalignment="top",
                bbox={"color": color, "pad": 0},
            )

    plt.axis("off")
    plt.gca().xaxis.set_major_locator(NullLocator())
    plt.gca().yaxis.set_major_locator(NullLocator())
    return fig

def cv2_put_text(img, text, text_offset_x, text_offset_y, font_scale = 0.35, background_color=(255, 255, 255), text_color=(255, 255, 255)):
    """
    A Function to write text on an image using openCV
    :param img: The image to write text on it
    :param text: The text to be written
    :param text_offset_x: The text bbox upper left point abscissa
    :param text_offset_y: The text bbox upper left point ordinate
    :param background_color: The text bbox background color
    :param text_color: The text color
    :return: Nothing
    """
    font = cv2.FONT_HERSHEY_SIMPLEX

    # get the width and height of the text box
    (text_width, text_height) = cv2.getTextSize(text, font, fontScale=font_scale, thickness=1)[0]

    # make the coords of the box with a small padding of two pixels
    box_coords = ((text_offset_x, text_offset_y), (text_offset_x + text_width + 2, text_offset_y - text_height - 2))
    cv2.rectangle(img, box_coords[0], box_coords[1], background_color, cv2.FILLED)
    cv2.putText(img, text, (text_offset_x, text_offset_y), font, fontScale=font_scale, color=text_color, thickness=1)
    
def annotate_frame_with_objects(original_frame, objects_bboxes, class_names, model_input_size,  only_classes=None,
                                confidence_threshold= 0, plot_labels=True, plot_class_confidence=False, text_color=(255,255,255),
                                thickness= 2, text_font_scale=0.5):
    """
    This function plots detected objects bounding boxes over images with class name and accuracy
    :param original_frame: A Frame(Image) from video
    :param objects_bboxes: Detected Objects Bounding boxes (output of yolo object detection model) and their class
    :param class_names: Array of class names
    :param only_classes: A list of class names to consider, if none consider all
    :param confidence_threshold:
    :param plot_labels: Whether to write down class label over bounding boxes or not
    :param plot_class_confidence: Whether to write down class confidence over bounding boxes or not
    :return: Masked Frame
    """
    masked_frame = copy.copy(original_frame)

    # Rescale boxes to original image
    if not (model_input_size, model_input_size) == masked_frame.shape[:2]:
        detections = rescale_boxes(objects_bboxes, model_input_size, masked_frame.shape[:2])
    else:
        detections = objects_bboxes

    # Create Colors
    cmap = plt.get_cmap("tab20b")
    colors = [cmap(i) for i in np.linspace(0, 1, 20)]
    unique_labels = detections[:, -1].cpu().unique()
    n_cls_preds = len(unique_labels)
    bbox_colors = random.sample(colors, n_cls_preds)

    for x1, y1, x2, y2, conf, cls_conf, cls_id in detections.cpu().data.numpy():
        x1 = int(x1)
        x2 = int(x2)
        y1 = int(y1)
        y2 = int(y2)
        conf = float(conf)
        cls_conf = float(cls_conf)
        cls_id = int(cls_id)

        if only_classes and not class_names[cls_id] in only_classes:
            continue

        if cls_conf<confidence_threshold:
            continue

        # Calculate the width and height of the bounding box relative to the size of the image.
        box_w = x2 - x1
        box_h = y2 - y1

        # get color
        bb_color = bbox_colors[int(np.where(unique_labels == int(cls_id))[0])]
        bb_color =  tuple([ int(ch*255) for ch in bb_color])[:3]

        cv2.rectangle( masked_frame, (x1, y1), ( x2, y2),
                      color = bb_color,
                      thickness=  thickness)

        if plot_labels:
            # Define x and y offsets for the labels
            lxc = (masked_frame.shape[1] * 0.266) / 100
            lyc = (masked_frame.shape[0] * 1.180) / 100

            # Plot class name
            cv2_put_text(masked_frame, class_names[cls_id], x1, y1-1, font_scale=text_font_scale, background_color=bb_color, text_color=text_color)

        if plot_class_confidence:
            # Plot probability
            cv2_put_text(masked_frame, "{0:.2f}".format(cls_conf), x1, y2, font_scale=text_font_scale,  background_color=bb_color, text_color=text_color)

    return masked_frame

def generate_yolo_train_test_files(images_dir, output_dir, classes, train_valid_split=0.8):

    train_output = output_dir+"/train.txt"
    valid_output = output_dir+"/valid.txt"
    data_output = output_dir+"/obj.data"
    names_output = output_dir+"/obj.names"
    backup_path = output_dir+"/backup"

    images = [ f for f in os.listdir(images_dir) if re.match(".*.jpg$",f)]
    train = np.random.choice(images, size=int(len(images)*train_valid_split) )
    valid = set(images) - set(train)

    # Write train.txt
    f_train = open(train_output, "w")
    for image_name in train:
        f_train.write(images_dir+"/"+image_name+"\n")
    f_train.close()

    # Write valid.txt
    f_valid = open(valid_output, "w")
    for image_name in valid:
        f_valid.write(images_dir+"/"+image_name+"\n")
    f_train.close()

    # create backup
    os.makedirs(backup_path)

    # Write obj.names
    f_names = open(names_output, "w")
    for class_name in classes:
        f_names.write(class_name+"\n")
    f_names.close()

    f_data = open(data_output, "w")
    f_data.write("classes="+str(len(classes))+"\n")
    f_data.write("train="+train_output+"\n")
    f_data.write("valid="+valid_output+"\n")
    f_data.write("names="+names_output+"\n")
    f_data.write("backup="+backup_path+"\n")
    f_data.close()


def replace_class_yolo_format(original_class, replace_class, images_labels_dir, image_label_file_regex=".*.txt$"):
    for file in os.listdir(images_labels_dir):
        if re.match(pattern=image_label_file_regex, string=file):

            # open file
            f = open(os.path.join(images_labels_dir, file), "r")
            file_content = f.read().split("\n")
            f.close()

            file_output_content = []
            for line in file_content:

                if line == "":
                    continue

                bbox = line.split(" ")
                if int(bbox[0]) == original_class:
                    bbox[0] = str(replace_class)
                file_output_content.append(" ".join(bbox))
            f = open(os.path.join(images_labels_dir, file), "w")
            f.write("\n".join(file_output_content)+"\n")
            f.close()
        else:
            continue
    

def normalize_cvat_labels(images_labels_dir, image_label_file_regex=".*.txt$"):

    lines_changed = 0 

    for file in os.listdir(images_labels_dir):

        if re.match(pattern=image_label_file_regex, string=file):

            # open file
            f = open(os.path.join(images_labels_dir, file), "r")
            file_content = f.read().split("\n")[:-1]
            f.close()

            file_output_content = []
            for line in file_content:
                data = line.split(" ")

                if (len(data)>5): # we have multiple bbox of same class as in cvat
                    lines_changed+=1
                    class_ = data[0]
                    bboxes = np.array(data[1:]).reshape( (-1,4) )
                    for bbox in bboxes:
                        file_output_content.append(" ".join( class_+ list(bbox) ) )

                else: # We have one bbox

                    file_output_content.append(" ".join(data))

            f = open(os.path.join(images_labels_dir, file), "w")
            f.write("\n".join(file_output_content))
            f.close()
        else:
            continue

    return lines_changed

def prepare_image_yolo(img, model_img_size, pad_value=0):

    """
    """
    c, h, w = img.shape
    dim_diff = np.abs(h - w)
    
    # (upper / left) padding and (lower / right) padding
    pad1, pad2 = dim_diff // 2, dim_diff - dim_diff // 2
    
    # Determine padding
    pad = (0, 0, pad1, pad2) if h <= w else (pad1, pad2, 0, 0)
    
    # Add padding
    img = F.pad(img, pad, "constant", value=pad_value)

    img = F.interpolate(img.unsqueeze(0), size=model_img_size, mode="nearest").squeeze(0)

    return img

    

