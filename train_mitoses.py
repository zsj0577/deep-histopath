"""Training - mitosis detection"""
import argparse
from datetime import datetime
import os
import pickle
import shutil

import keras
from keras import backend as K
from keras.applications.vgg16 import VGG16
#from keras.applications.resnet50 import ResNet50
from resnet50 import ResNet50
from keras.initializers import VarianceScaling
from keras.layers import Dense, Dropout, Flatten, GlobalAveragePooling2D, Input
from keras.models import Model
import numpy as np
import tensorflow as tf


def get_label(filename):
  """Get label from filename.

  Args:
    filename: String in format "**/train|val/mitosis|normal/name.{ext}",
      where the label is either "mitosis" or "normal".

  Returns:
    TensorFlow float binary label equal to 1 for mitosis or 0 for
    normal.
  """
  # note file name format:
  # lab is a single digit, case and region are two digits with padding if needed
  splits = tf.string_split([filename], "/")
  label_str = splits.values[-2]
  # check that label string is valid
  is_valid = tf.logical_or(tf.equal(label_str, 'normal'), tf.equal(label_str, 'mitosis'))
  assert_op = tf.Assert(is_valid, [label_str])
  with tf.control_dependencies([assert_op]):  # test for correct label extraction
    #label = tf.to_int32(tf.equal(label_str, 'mitosis'))
    label = tf.to_float(tf.equal(label_str, 'mitosis'))  # required because model produces float
    return label


def get_image(filename, patch_size):
  """Get image from filename.

  Args:
    filename: String filename of an image.
    patch_size: Integer length to which the square image will be
      resized.

  Returns:
    TensorFlow tensor containing the decoded and resized image with
    type float32 and values in [0, 1).
  """
  image_string = tf.read_file(filename)
  image = tf.image.decode_jpeg(image_string, channels=3)  # shape (h,w,c), uint8 in [0, 255]
  image = tf.image.convert_image_dtype(image, dtype=tf.float32)  # float32 [0, 1)
  image = tf.image.resize_images(image, [patch_size, patch_size])  # float32 [0, 1)
  #with tf.control_dependencies([tf.assert_type(image, tf.float32, image.dtype)]):
  return image


def preprocess(filename, patch_size, augmentation, model_name):
  """Get image and label from filename.

  Args:
    filename: String filename of an image.
    patch_size: Integer length to which the square image will be
      resized.
    augmentation: Boolean for whether or not to apply random augmentation
      to the image.
    model_name: String indicating the model to use.

  Returns:
    Tuple of a TensorFlow image tensor, a binary label, and a filename.
  """
  #  return image_resized, label
  label = get_label(filename)
  #label = tf.expand_dims(label, -1)  # make each scalar label a vector of length 1 to match model
  image = get_image(filename, patch_size)  # float32 in [0, 1)
  if augmentation:
    image = augment(image)
    image = tf.clip_by_value(image, 0, 1)
  image = normalize(image, model_name)
  return image, label, filename


def normalize(image, model_name):
  """Normalize an image.

  Args:
    image: A Tensor of shape (h,w,c) with values in [0, 1].
    model_name: String indicating the model to use.

  Returns:
    A normalized image Tensor.
  """
  if model_name in ("vgg", "resnet"):
    image = image * 255.0  # float32 in [0, 255]
    image = image[..., ::-1]  # rbg -> bgr
    image = image - [103.939, 116.779, 123.68]  # mean centering using imagenet means
  else:
    #image = image / 255.
    image = image - 0.5
    image = image * 2.
  return image


def unnormalize(image, model_name):
  """Unnormalize an image.

  Args:
    image: A Tensor of shape (...,h,w,c) with normalized values.
    model_name: String indicating the model to use.

  Returns:
    An unnormalized image Tensor of shape (...,h,w,c) with values in
    [0, 1].
  """
  if model_name in ("vgg", "resnet"):
    image = image + [103.939, 116.779, 123.68]  # mean centering using imagenet means
    image = image[..., ::-1]  # bgr -> rgb
    image = image / 255.0  # float32 in [0, 1]
  else:
    image = image / 2.
    image = image + 0.5
  return image


def augment(image, seed=None):
  """Apply random data augmentation to the given image.

  Args:
    image: A Tensor of shape (h,w,c).
    seed: An integer used to create a random seed.

  Returns:
    A data-augmented image.
  """
  # NOTE: these values currently come from the Google pathology paper:
  # Liu Y, Gadepalli K, Norouzi M, Dahl GE, Kohlberger T, Boyko A, et al.
  # Detecting Cancer Metastases on Gigapixel Pathology Images. arXiv.org. 2017.
  image = tf.image.random_flip_up_down(image, seed=seed)
  image = tf.image.random_flip_left_right(image, seed=seed)
  image = tf.image.random_brightness(image, 64/255, seed=seed)
  image = tf.image.random_contrast(image, 0.25, 1, seed=seed)
  image = tf.image.random_saturation(image, 0.75, 1, seed=seed)
  image = tf.image.random_hue(image, 0.04, seed=seed)
  return image


def create_augmented_batch(image, batch_size):
  """Create a batch of augmented versions of the given image.

  This will sample `batch_size/4` augmented images deterministically,
  and yield four rotated variants for each augmented image (0, 90, 180,
  270 degrees).

  Args:
    image: A Tensor of shape (h,w,c).
    batch_size: Number of augmented versions to generate.

  Returns:
    A Tensor containing a batch of `batch_size` data-augmented versions
    of the given image.
  """
  assert batch_size % 4 == 0 or batch_size == 1, "batch_size must be 1 or divisible by 4"

  def rots_batch(image):
    rot0 = image
    rot90 = tf.image.rot90(image)
    rot180 = tf.image.rot90(image, k=2)
    rot270 = tf.image.rot90(image, k=3)
    rots = tf.stack([rot0, rot90, rot180, rot270])
    return rots

  if batch_size >= 4:
    images = rots_batch(image)
    for i in range(round(batch_size/4)-1):
      aug_image = augment(image, i)
      aug_image_rots = rots_batch(aug_image)
      images = tf.concat([images, aug_image_rots], axis=0)
  else:
    images = tf.expand_dims(image, 0)

  return images


def create_reset_metric(metric, scope, **metric_kwargs):  # prob safer to only allow kwargs
  """Create a resettable metric.

  Args:
    metric: A tf.metrics metric function.
    scope: A String scope name to enclose the metric variables within.
    metric_kwargs:  Kwargs for the metric.

  Returns:
    The metric op, the metric update op, and a metric reset op.
  """
  # started with an implementation from https://github.com/tensorflow/tensorflow/issues/4814
  with tf.variable_scope(scope) as scope:
    metric_op, update_op = metric(**metric_kwargs)
    scope_name = tf.contrib.framework.get_name_scope()  # in case nested name/variable scopes
    local_vars = tf.contrib.framework.get_variables(scope_name,
        collection=tf.GraphKeys.LOCAL_VARIABLES)  # get all local variables in this scope
    reset_op = tf.variables_initializer(local_vars)
  return metric_op, update_op, reset_op


def initialize_variables(sess):
  """Initialize variables for training.

  This initializes all tensor variables in the graph, as well as
  variables for the global step and epoch, the latter two of which
  are returned as native Python variables.

  Args:
    sess: A TensorFlow Session.

  Returns:
    Integer global step and global epoch values.
  """
  # NOTE: Keras keeps track of the variables that are initialized, and any call to
  # `K.get_session()`, which is even used internally, will include logic to initialize variables.
  # There is a situation in which resuming from a previous checkpoint and then saving the model
  # after the first epoch will result in part of the model being reinitialized.  The problem is
  # that calling `K.get_session()` here is too soon to initialize any variables, the resume branch
  # skips any variable initialization, and then the `model.save` code path ends up calling
  # `K.get_session()`, thus causing part of the model to be reinitialized.  Specifically, the model
  # base is fine because it is initialized when the pretrained weights are added in, but the new
  # dense classifier will not be marked as initialized by Keras.  The non-resume branch will
  # initialize any variables not initialized by Keras yet, and thus will avoid this issue.  It
  # could be possible to use `K.manual_variable_initialization(True)` and then manually initialize
  # all variables, but this would cause any pretrained weights to be removed.  Instead, we should
  # initialize all variables first with the equivalent of the logic in `K.get_session()`, and then
  # call resume.
  # NOTE: the global variables initializer will erase the pretrained weights, so we instead only
  # initialize the other variables
  # NOTE: reproduced from the old K._initialize_variables() function
  # EDIT: this was updated in the master branch in commit
  # https://github.com/fchollet/keras/commit/9166733c3c144739868fe0c30d57b861b4947b44
  # TODO: given the change in master, reevaluate whether or not this is actually necessary anymore
  variables = tf.global_variables()
  uninitialized_variables = []
  for v in variables:
    if not hasattr(v, '_keras_initialized') or not v._keras_initialized:
      uninitialized_variables.append(v)
      v._keras_initialized = True
  global_init_op = tf.variables_initializer(uninitialized_variables)
  local_init_op = tf.local_variables_initializer()
  sess.run([global_init_op, local_init_op])
  global_step = 0  # training step
  global_epoch = 0  # training epoch
  return global_step, global_epoch


def compute_l2_reg(model):
  """Compute L2 loss of trainable model weights, excluding biases.

  Args:
    model: A Keras Model object.

  Returns:
    The L2 regularization loss of all trainable model weights.
  """
  weights = []
  for layer in model.layers:
    if layer.trainable:
      if hasattr(layer, 'kernel'):
        weights.append(layer.kernel)
  l2_loss = tf.add_n([tf.nn.l2_loss(w) for w in weights])
  return l2_loss


def train(train_path, val_path, exp_path, model_name, patch_size, train_batch_size, val_batch_size,
    clf_epochs, finetune_epochs, clf_lr, finetune_lr, finetune_momentum, finetune_layers, l2,
    augmentation, marginalization, log_interval, threads, checkpoint, resume):
  """Train a model.

  Args:
    train_path: String path to the generated training image patches.
      This should contain folders for each class.
    val_path: String path to the generated validation image patches.
      This should contain folders for each class.
    exp_path: String path in which to store the model checkpoints, logs,
      etc. for this experiment
    model_name: String indicating the model to use.
    patch_size: Integer length to which the square patches will be
      resized.
    train_batch_size: Integer training batch size.
    val_batch_size: Integer validation batch size.
    clf_epochs: Integer number of epochs for which to training the new
      classifier layers.
    finetune_epochs: Integer number of epochs for which to fine-tune the
      model.
    clf_lr: Float learning rate for training the new classifier layers.
    finetune_lr: Float learning rate for fine-tuning the model.
    finetune_momentum: Float momentum rate for fine-tuning the model.
    finetune_layers: Integer number of layers at the end of the
      pretrained portion of the model to fine-tune.  The new classifier
      layers will still be trained during fine-tuning as well.
    l2: Float L2 global regularization value.
    augmentation: Boolean for whether or not to apply random augmentation
      to the image.
    marginalization: Boolean for whether or not to use noise
      marginalization when evaluating the validation set.  If True, then
      each image in the validation set will be expanded to a batch of
      augmented versions of that image, and predicted probabilities for
      each batch will be averaged to yield a single noise-marginalized
      prediction for each image.  Note: if this is True, then
      `val_batch_size` must be divisible by 4, or equal to 1 for a
      special debugging case of no augmentation.
    log_interval: Integer number of steps between logging during
      training.
    threads: Integer number of threads for dataset buffering.
    checkpoint: Boolean flag for whether or not to save a checkpoint
      after each epoch.
    resume: Boolean flag for whether or not to resume training from a
      checkpoint.
  """
  # TODO: break this out into:
  #   * data gen func
  #   * inference func
  #   * loss func
  #   * metrics func
  #   * logging func
  #   * train func

  # data
  with tf.name_scope("data"):
    # TODO: add data augmentation function
    train_dataset = (tf.contrib.data.Dataset.list_files('{}/*/*.jpg'.format(train_path))
        .shuffle(500000)
        .map(lambda filename: preprocess(filename, patch_size, augmentation, model_name),
          num_threads=threads, output_buffer_size=100*train_batch_size)
        .batch(train_batch_size))
    if marginalization:
      val_dataset = (tf.contrib.data.Dataset.list_files('{}/*/*.jpg'.format(val_path))
          .map(lambda filename: preprocess(filename, patch_size, False, model_name))
          .map(lambda image, label, filename:
              (create_augmented_batch(image, val_batch_size),
               tf.tile(tf.expand_dims(label, -1), [val_batch_size]),
               tf.tile(tf.expand_dims(filename, -1), [val_batch_size])),
            num_threads=threads, output_buffer_size=25*val_batch_size))
    else:
      val_dataset = (tf.contrib.data.Dataset.list_files('{}/*/*.jpg'.format(val_path))
          .map(lambda filename: preprocess(filename, patch_size, False, model_name),
            num_threads=threads, output_buffer_size=100*val_batch_size)
          .batch(val_batch_size))

    iterator = tf.contrib.data.Iterator.from_structure(train_dataset.output_types,
                                                       train_dataset.output_shapes)
    images, labels, filenames = iterator.get_next()
    actual_batch_size = tf.shape(images)[0]
    percent_pos = tf.reduce_mean(labels)  # positive labels are 1
    pos_mask = tf.cast(labels, tf.bool)
    neg_mask = tf.logical_not(pos_mask)
    mitosis_images = tf.boolean_mask(images, pos_mask)
    normal_images = tf.boolean_mask(images, neg_mask)
    mitosis_filenames = tf.boolean_mask(filenames, pos_mask)
    normal_filenames = tf.boolean_mask(filenames, neg_mask)
    input_shape = (patch_size, patch_size, 3)
    train_init_op = iterator.make_initializer(train_dataset)
    val_init_op = iterator.make_initializer(val_dataset)

  # models
  with tf.name_scope("model"):
    if model_name == "logreg":
      # logistic regression classifier
      model_base = keras.models.Sequential()  # dummy since we aren't fine-tuning this model
      inputs = Input(shape=input_shape, tensor=images)
      x = Flatten()(inputs)
      # init Dense weights with Gaussian scaled by sqrt(2/(fan_in+fan_out))
      logits = Dense(1, kernel_initializer="glorot_normal")(x)
      model_tower = Model(inputs=inputs, outputs=logits, name="model")

    elif model_name == "vgg":
      # create a model by replacing the classifier of a VGG16 model with a new classifier specific
      # to the breast cancer problem
      # recommend fine-tuning last 4 layers
      #with tf.device("/cpu"):
      #inputs = Input(shape=input_shape)
      model_base = VGG16(include_top=False, input_shape=input_shape, input_tensor=images)  #inputs)
      inputs = model_base.inputs
      x = model_base.output
      x = Flatten()(x)
      #x = GlobalAveragePooling2D()(x)
      #x = Dropout(0.5)(x)
      #x = Dropout(0.1)(x)
      #x = Dense(256, activation='relu', name='fc1')(x)
      #x = Dense(256, kernel_initializer="he_normal",
      #    kernel_regularizer=keras.regularizers.l2(l2))(x)
      #x = Dropout(0.5)(x)
      #x = Dropout(0.1)(x)
      #x = Dense(256, activation='relu', name='fc2')(x)
      #x = Dense(256, kernel_initializer="he_normal",
      #    kernel_regularizer=keras.regularizers.l2(l2))(x)
      # init Dense weights with Gaussian scaled by sqrt(2/(fan_in+fan_out))
      logits = Dense(1, kernel_initializer="glorot_normal")(x)
      model_tower = Model(inputs=inputs, outputs=logits, name="model")

    elif model_name == "resnet":
      # create a model by replacing the classifier of a ResNet50 model with a new classifier
      # specific to the breast cancer problem
      # recommend fine-tuning last 11 (stage 5 block c), 21 (stage 5 blocks b & c), or 33 (stage
      # 5 blocks a,b,c) layers
      #with tf.device("/cpu"):
      # NOTE: there is an issue in keras with using batch norm with model templating, i.e.,
      # defining a model with generic inputs and then calling it on a tensor.  the issue stems from
      # batch norm not being well defined for shared settings, but it makes it quite annoying in
      # this context.  to "fix" it, we define it by directly passing in the `images` tensor
      # https://github.com/fchollet/keras/issues/2827
      # TODO: find out if it will be possible to save this model and load it in a different context
      #inputs = Input(shape=input_shape)
      model_base = ResNet50(include_top=False, input_shape=input_shape, input_tensor=images) #inputs)
      inputs = model_base.inputs
      x = model_base.output
      x = Flatten()(x)
      #x = GlobalAveragePooling2D()(x)
      # init Dense weights with Gaussian scaled by sqrt(2/(fan_in+fan_out))
      logits = Dense(1, kernel_initializer="glorot_normal")(x)
      model_tower = Model(inputs=inputs, outputs=logits, name="model")

    else:
      raise Exception("model name unknown: {}".format(model_name))

    # TODO: add this when it's necessary, and move to a separate function
    ## Multi-GPU exploitation via a linear combination of GPU loss functions.
    #ins = []
    #outs = []
    #for i in range(num_gpus):
    #  with tf.device("/gpu:{}".format(i)):
    #    x = Input(shape=input_shape)  # split of batch
    #    out = resnet50(x)  # run split on shared model
    #    ins.append(x)
    #    outs.append(out)
    #model = Model(inputs=ins, outputs=outs)  # multi-GPU, data-parallel model
    model = model_tower

    # unfreeze all model layers.
    for layer in model.layers[1:]:  # don't include input layer
      layer.trainable = True

    # compute logits and predictions
    # NOTE: tf prefers to feed logits into a combined sigmoid and logistic loss function for
    # numerical stability
    # NOTE: preds has an implicit threshold at 0.5
    logits = model.output
    # use noise marginalization at test time, i.e., average predictions over a batch of augmented
    # versions of a single image
    avg_logits = tf.reduce_mean(logits, axis=0, keep_dims=True, name="avg_logits")
    logits = tf.cond(tf.logical_and(marginalization, tf.logical_not(K.learning_phase())),
        lambda: avg_logits, lambda: logits)
    probs = tf.nn.sigmoid(logits, name="probs")
    preds = tf.round(probs, name="preds")
    num_preds = tf.shape(preds)[0]

    # false-positive & false-negative cases
    pos_preds_mask = tf.cast(tf.squeeze(preds, axis=1), tf.bool)
    neg_preds_mask = tf.logical_not(pos_preds_mask)
    fp_mask = tf.logical_and(pos_preds_mask, neg_mask)
    fn_mask = tf.logical_and(neg_preds_mask, pos_mask)
    fp_images = tf.boolean_mask(images, fp_mask)
    fn_images = tf.boolean_mask(images, fn_mask)
    fp_filenames = tf.boolean_mask(filenames, fp_mask)
    fn_filenames = tf.boolean_mask(filenames, fn_mask)

  # loss
  with tf.name_scope("loss"):
    # use noise marginalization at test time
    labels = tf.cond(tf.logical_and(marginalization, tf.logical_not(K.learning_phase())),
        lambda: labels[0], lambda: labels)
    base_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
      labels=tf.reshape(labels, [-1, 1]), logits=logits))
    loss = base_loss + l2*compute_l2_reg(model)  # including all weight regularization

  # optim
  with tf.name_scope("optim"):
    # classifier
    # - freeze all pre-trained model layers.
    for layer in model_base.layers:
      layer.trainable = False
    # add any weight regularization to the base loss for unfrozen layers:
    clf_loss = base_loss + l2*compute_l2_reg(model)  # including all weight regularization
    clf_opt = tf.train.AdamOptimizer(clf_lr)
    clf_grads_and_vars = clf_opt.compute_gradients(clf_loss, var_list=model.trainable_weights)
    #clf_train_op = opt.minimize(clf_loss, var_list=model.trainable_weights)
    clf_apply_grads_op = clf_opt.apply_gradients(clf_grads_and_vars)
    clf_model_update_ops = model.updates
    clf_train_op = tf.group(clf_apply_grads_op, *clf_model_update_ops)

    # finetuning
    # - unfreeze a portion of the pre-trained model layers.
    # note, could make this arbitrary, but for now, fine-tune some number of layers at the *end* of
    # the pretrained portion of the model
    if finetune_layers > 0:
      for layer in model_base.layers[-finetune_layers:]:
        layer.trainable = True
    # add any weight regularization to the base loss for unfrozen layers:
    finetune_loss = base_loss + l2*compute_l2_reg(model)  # including all weight regularization
    finetune_opt = tf.train.MomentumOptimizer(finetune_lr, finetune_momentum, use_nesterov=True)
    finetune_grads_and_vars = finetune_opt.compute_gradients(finetune_loss,
        var_list=model.trainable_weights)
    #finetune_train_op = opt.minimize(finetune_loss, var_list=model.trainable_weights)
    finetune_apply_grads_op = finetune_opt.apply_gradients(finetune_grads_and_vars)
    finetune_model_update_ops = model.updates
    finetune_train_op = tf.group(finetune_apply_grads_op, *finetune_model_update_ops)

  # metrics
  with tf.name_scope("metrics"):
    mean_loss, mean_loss_update_op, mean_loss_reset_op = create_reset_metric(tf.metrics.mean,
        'mean_loss', values=loss)
    acc, acc_update_op, acc_reset_op = create_reset_metric(tf.metrics.accuracy,
        'acc', labels=labels, predictions=preds)
    ppv, ppv_update_op, ppv_reset_op = create_reset_metric(tf.metrics.precision,
        'ppv', labels=labels, predictions=preds)
    sens, sens_update_op, sens_reset_op = create_reset_metric(tf.metrics.recall,
        'sens', labels=labels, predictions=preds)
    f1 = 2 * (ppv * sens) / (ppv + sens)

    # combine all reset & update ops
    metric_update_ops = tf.group(mean_loss_update_op, acc_update_op, ppv_update_op, sens_update_op)
    metric_reset_ops = tf.group(mean_loss_reset_op, acc_reset_op, ppv_reset_op, sens_reset_op)

  # tensorboard summaries
  # NOTE: tensorflow is annoying when it comes to name scopes, so sometimes the name needs to be
  # hardcoded as a prefix instead of a proper name scope if that name was used as a name scope
  # earlier. otherwise, a numeric suffix will be appended to the name.
  # general minibatch summaries
  with tf.name_scope("images"):
    tf.summary.image("mitosis", unnormalize(mitosis_images, model_name), 1,
        collections=["minibatch", "minibatch_val"])
    tf.summary.image("normal", unnormalize(normal_images, model_name), 1,
        collections=["minibatch", "minibatch_val"])
    tf.summary.image("false-positive", unnormalize(fp_images, model_name), 1,
        collections=["minibatch", "minibatch_val"])
    tf.summary.image("false-negative", unnormalize(fn_images, model_name), 1,
        collections=["minibatch", "minibatch_val"])
  with tf.name_scope("data/filenames"):
    tf.summary.text("mitosis", mitosis_filenames, collections=["minibatch", "minibatch_val"])
    tf.summary.text("normal", normal_filenames, collections=["minibatch", "minibatch_val"])
    tf.summary.text("false-positive", fp_filenames, collections=["minibatch", "minibatch_val"])
    tf.summary.text("false-negative", fn_filenames, collections=["minibatch", "minibatch_val"])
  tf.summary.histogram("data/images", images, collections=["minibatch", "minibatch_val"])
  tf.summary.histogram("data/labels", labels, collections=["minibatch", "minibatch_val"])
  for layer in model.layers:
    for weight in layer.weights:
      tf.summary.histogram(weight.name, weight, collections=["minibatch"])
    if hasattr(layer, 'output'):
      layer_name = "model/{}/out".format(layer.name)
      tf.summary.histogram(layer_name, layer.output, collections=["minibatch"])
  tf.summary.histogram("model/probs", probs, collections=["minibatch"])
  tf.summary.histogram("model/preds", preds, collections=["minibatch"])
  with tf.name_scope("minibatch"):
    tf.summary.scalar("loss", loss, collections=["minibatch"])
    tf.summary.scalar("batch_size", actual_batch_size, collections=["minibatch", "minibatch_val"])
    tf.summary.scalar("num_preds", num_preds, collections=["minibatch", "minibatch_val"])
    tf.summary.scalar("percent_positive", percent_pos, collections=["minibatch"])
    tf.summary.scalar("learning_phase", tf.to_int32(K.learning_phase()),
        collections=["minibatch", "minibatch_val"])
  # TODO: gradient histograms
  # TODO: first layer convolution kernels as images
  minibatch_summaries = tf.summary.merge_all("minibatch")
  minibatch_val_summaries = tf.summary.merge_all("minibatch_val")

  # epoch summaries
  with tf.name_scope("epoch"):
    tf.summary.scalar("loss", mean_loss, collections=["epoch"])
    tf.summary.scalar("acc", acc, collections=["epoch"])
    tf.summary.scalar("ppv", ppv, collections=["epoch"])
    tf.summary.scalar("sens", sens, collections=["epoch"])
    tf.summary.scalar("f1", f1, collections=["epoch"])
  epoch_summaries = tf.summary.merge_all("epoch")

  # use train and val writers so that plots can be on same graph
  train_writer = tf.summary.FileWriter(os.path.join(exp_path, "train"), tf.get_default_graph())
  val_writer = tf.summary.FileWriter(os.path.join(exp_path, "val"))

  # save ops
  checkpoint_filename = os.path.join(exp_path, "model.ckpt")
  global_step_epoch_filename = os.path.join(exp_path, "global_step_epoch.pickle")
  saver = tf.train.Saver()

  # initialize stuff
  sess = K.get_session()
  global_step, global_epoch = initialize_variables(sess)

  # debugger
  #from tensorflow.python import debug as tf_debug
  #sess = tf_debug.LocalCLIDebugWrapperSession(sess)

  if resume:
    saver.restore(sess, checkpoint_filename)
    with open(global_step_epoch_filename, "rb") as f:
      global_step, global_epoch = pickle.load(f)

  # new classifier layers + fine-tuning combined training loop
  for train_op, epochs in [(clf_train_op, clf_epochs), (finetune_train_op, finetune_epochs)]:
    for _ in range(global_epoch, global_epoch+epochs):  # allow for resuming of training
      # training
      sess.run(train_init_op)
      while True:
        try:
          if log_interval > 0 and global_step % log_interval == 0:
            # train, update metrics, & log stuff
            _, _, loss_val, mean_loss_val, f1_val, summary_str = sess.run([train_op,
                metric_update_ops, loss, mean_loss, f1, minibatch_summaries],
                feed_dict={K.learning_phase(): 1})
            train_writer.add_summary(summary_str, global_step)
            print("train", global_epoch, global_step, loss_val, mean_loss_val, f1_val)
          else:
            # train & update metrics
            _, _ = sess.run([train_op, metric_update_ops], feed_dict={K.learning_phase(): 1})
          global_step += 1
        except tf.errors.OutOfRangeError:
          break
      # log average training metrics for epoch & reset
      mean_loss_val, f1_val, summary_str = sess.run([mean_loss, f1, epoch_summaries])
      print("---epoch {}, train avg loss: {}, train f1: {}".format(global_epoch, mean_loss_val,
          f1_val))
      train_writer.add_summary(summary_str, global_epoch)
      sess.run(metric_reset_ops)

      # validation
      sess.run(val_init_op)
      vi = 0  # validation step
      while True:
        try:
          # evaluate & update metrics
          if log_interval > 0 and vi % log_interval == 0:
            _, loss_val, mean_loss_val, f1_val, summary_str = sess.run([metric_update_ops, loss,
                mean_loss, f1, minibatch_val_summaries], feed_dict={K.learning_phase(): 0})
            print("val", global_epoch, vi, loss_val, mean_loss_val, f1_val)
            val_writer.add_summary(summary_str, vi)
          else:
            _ = sess.run(metric_update_ops, feed_dict={K.learning_phase(): 0})
          vi += 1
        except tf.errors.OutOfRangeError:
          break
      # log average validation metrics for epoch & reset
      mean_loss_val, f1_val, summary_str = sess.run([mean_loss, f1, epoch_summaries])
      print("---epoch {}, val avg loss: {}, val f1: {}".format(global_epoch, mean_loss_val, f1_val))
      val_writer.add_summary(summary_str, global_epoch)
      sess.run(metric_reset_ops)

      val_writer.flush()
      #train_writer.flush()

      global_epoch += 1

      # save model
      if checkpoint:
        keras_filename = os.path.join(exp_path, "checkpoints",
            f"{f1_val:.5}_f1_{mean_loss_val:.5}_loss_{global_epoch}_epoch_model.hdf5")
        model.save(keras_filename, include_optimizer=False)  # keras model
        saver.save(sess, checkpoint_filename)  # full TF graph
        with open(global_step_epoch_filename, "wb") as f:
          pickle.dump((global_step, global_epoch), f)  # step & epoch
        print("Saved model file to {}".format(keras_filename))


if __name__ == "__main__":
  # parse args
  parser = argparse.ArgumentParser()
  parser.add_argument("--patches_path", default=os.path.join("data", "mitoses", "patches"),
      help="path to the generated image patches containing `train` & `val` folders "\
           "(default: %(default)s)")
  parser.add_argument("--exp_parent_path", default=os.path.join("experiments", "mitoses"),
      help="parent path in which to store experiment folders (default: %(default)s)")
  parser.add_argument("--exp_name", default=None,
      help="path within the experiment parent path in which to store the model checkpoints, "\
           "logs, etc. for this experiment; an existing path can be used to resume training "\
           "(default: %%y-%%m-%%d_%%H:%%M:%%S_{model})")
  parser.add_argument("--exp_name_suffix", default=None,
      help="suffix to add to experiment name (default: all parameters concatenated together)")
  parser.add_argument("--model", default="vgg",
      help="name of the model to use in ['logreg', 'vgg', 'resnet'] (default: %(default)s)")
  parser.add_argument("--patch_size", type=int, default=64,
      help="integer length to which the square patches will be resized (default: %(default)s)")
  parser.add_argument("--train_batch_size", type=int, default=32,
      help="training batch size (default: %(default)s)")
  parser.add_argument("--val_batch_size", type=int, default=32,
      help="validation batch size (default: %(default)s)")
  parser.add_argument("--clf_epochs", type=int, default=1,
      help="number of epochs for which to train the new classifier layers "\
           "(default: %(default)s)")
  parser.add_argument("--finetune_epochs", type=int, default=0,
      help="number of epochs for which to fine-tune the unfrozen layers (default: %(default)s)")
  parser.add_argument("--clf_lr", type=float, default=1e-3,
      help="learning rate for training the new classifier layers (default: %(default)s)")
  parser.add_argument("--finetune_lr", type=float, default=1e-4,
      help="learning rate for fine-tuning the unfrozen layers (default: %(default)s)")
  parser.add_argument("--finetune_momentum", type=float, default=0.9,
      help="momentum rate for fine-tuning the unfrozen layers (default: %(default)s)")
  parser.add_argument("--finetune_layers", type=int, default=0,
      help="number of layers at the end of the pretrained portion of the model to fine-tune "\
          "(note: the new classifier layers will still be trained during fine-tuning as well) "\
          "(default: %(default)s)")
  parser.add_argument("--l2", type=float, default=1e-3,
      help="amount of l2 weight regularization (default: %(default)s)")
  augment_parser = parser.add_mutually_exclusive_group(required=False)
  augment_parser.add_argument("--augment", dest="augment", action="store_true",
      help="apply random augmentation to the training images (default: True)")
  augment_parser.add_argument("--no_augment", dest="augment", action="store_false",
      help="do not apply random augmentation to the training images (default: False)")
  parser.set_defaults(augment=True)
  parser.add_argument("--marginalize", default=False, action="store_true",
      help="use noise marginalization when evaluating the validation set. if this is set, then " \
           "the validation batch_size must be divisible by 4, or equal to 1 for no augmentation "\
           "(default: %(default)s)")
  parser.add_argument("--log_interval", type=int, default=100,
      help="number of steps between logging during training (default: %(default)s)")
  parser.add_argument("--threads", type=int, default=5,
      help="number of threads for dataset buffering (default: %(default)s)")
  parser.add_argument("--resume", default=False, action="store_true",
      help="resume training from a checkpoint (default: %(default)s)")
  checkpoint_parser = parser.add_mutually_exclusive_group(required=False)
  checkpoint_parser.add_argument("--checkpoint", dest="checkpoint", action="store_true",
      help="save a checkpoint after each epoch (default: True)")
  checkpoint_parser.add_argument("--no_checkpoint", dest="checkpoint", action="store_false",
      help="do not save a checkpoint after each epoch (default: False)")
  parser.set_defaults(checkpoint=True)

  args = parser.parse_args()

  # set any other defaults
  train_path = os.path.join(args.patches_path, "train")
  val_path = os.path.join(args.patches_path, "val")

  if args.exp_name is None:
    date = datetime.strftime(datetime.today(), "%y%m%d_%H%M%S")
    args.exp_name = date + "_" + args.model
  if args.exp_name_suffix is None:
    args.exp_name_suffix = f"patch_size_{args.patch_size}_batch_size_{args.train_batch_size}_"\
                           f"clf_epochs_{args.clf_epochs}_finetune_epochs_{args.finetune_epochs}_"\
                           f"clf_lr_{args.clf_lr}_finetune_lr_{args.finetune_lr}_finetune_"\
                           f"momentum_{args.finetune_momentum}_finetune_layers_"\
                           f"{args.finetune_layers}_l2_{args.l2}_aug_{args.augment}_marg_"\
                           f"{args.marginalize}"
  full_exp_name = args.exp_name + "_" + args.exp_name_suffix
  exp_path = os.path.join(args.exp_parent_path, full_exp_name)

  # make an experiment folder
  if not os.path.exists(exp_path):
    os.makedirs(os.path.join(exp_path, "checkpoints"))
  print("experiment directory: {}".format(exp_path))

  # save args to a file in the experiment folder, appending if it exists
  with open(os.path.join(exp_path, 'args.txt'), 'a') as f:
    f.write(str(args) + "\n")

  # copy this script to the experiment folder
  shutil.copy2(os.path.realpath(__file__), exp_path)

  # train!
  train(train_path, val_path, exp_path, args.model, args.patch_size, args.train_batch_size,
      args.val_batch_size, args.clf_epochs, args.finetune_epochs, args.clf_lr, args.finetune_lr,
      args.finetune_momentum, args.finetune_layers, args.l2, args.augment, args.marginalize,
      args.log_interval, args.threads, args.checkpoint, args.resume)


# ---
# tests
# TODO: eventually move these to a separate file.
# `py.test train_mitoses.py`

# TODO: use this fixture when we move these tests to a test module
#import pytest
#
#@pytest.fixture(autouse=True)
#def reset():
#  """Ensure that the TensorFlow graph/session are clean."""
#  tf.reset_default_graph()
#  K.clear_session()
#  yield  # run test

def reset():
  """Ensure that the TensorFlow graph/session are clean."""
  tf.reset_default_graph()
  K.clear_session()

def test_get_label():
  import pytest

  # mitosis
  reset()
  filename = "train/mitosis/1_03_05_713_348.jpg"
  label_op = get_label(filename)
  sess = K.get_session()
  label = sess.run(label_op)
  assert label == 1

  # normal
  reset()
  filename = "train/normal/1_03_05_713_348.jpg"
  label_op = get_label(filename)
  sess = K.get_session()
  label = sess.run(label_op)
  assert label == 0

  # wrong label name
  with pytest.raises(tf.errors.InvalidArgumentError):
    reset()
    filename = "train/unknown/1_03_05_713_348.jpg"
    label_op = get_label(filename)
    sess = K.get_session()
    label = sess.run(label_op)


def test_resettable_metric():
  reset()
  x = tf.placeholder(tf.int32, [None, 1])
  x1 = np.array([1,0,0,0]).reshape(4,1)
  x2 = np.array([0,0,0,0]).reshape(4,1)

  with tf.name_scope("something"):  # testing nested name/variable scopes
    mean_op, update_op, reset_op = create_reset_metric(tf.metrics.mean, 'mean_loss', values=x)

  sess = K.get_session()
  sess.run(tf.global_variables_initializer())
  sess.run(tf.local_variables_initializer())

  _, out_up = sess.run([mean_op, update_op], feed_dict={x: x1})
  assert np.allclose([out_up], [1/4])
  assert np.allclose([sess.run(mean_op)], [1/4])
  assert np.allclose([sess.run(mean_op)], [1/4])

  _, out_up = sess.run([mean_op, update_op], feed_dict={x: x1})
  assert np.allclose([out_up], [2/8])
  assert np.allclose([sess.run(mean_op)], [2/8])
  assert np.allclose([sess.run(mean_op)], [2/8])

  _, out_up = sess.run([mean_op, update_op], feed_dict={x: x2})
  assert np.allclose([out_up], [2/12])
  assert np.allclose([sess.run(mean_op)], [2/12])
  assert np.allclose([sess.run(mean_op)], [2/12])

  sess.run(reset_op)  # make sure this works!

  _, out_up = sess.run([mean_op, update_op], feed_dict={x: x2})
  assert out_up == 0
  assert sess.run(mean_op) == 0

  _, out_up = sess.run([mean_op, update_op], feed_dict={x: x1})
  assert np.allclose([out_up], [1/8])
  assert np.allclose([sess.run(mean_op)], [1/8])
  assert np.allclose([sess.run(mean_op)], [1/8])


def test_initialize_variables():
  # NOTE: keep the `K.get_session()` call up here to ensure that the `initialize_variables` function
  # is working properly.
  #tf.reset_default_graph()
  ##K.manual_variable_initialization(True)
  #K.clear_session()  # this is needed if we want to create sessions at the beginning
  reset()
  sess = K.get_session()

  # create model with a mix of pretrained and new weights
  # NOTE: the pretrained layers will be initialized by Keras on creation, while the new Dense
  # layer will remain uninitialized
  input_shape = (224,224,3)
  inputs = Input(shape=input_shape)
  model_base = VGG16(include_top=False, input_shape=input_shape, input_tensor=inputs)
  x = model_base.output
  x = GlobalAveragePooling2D()(x)
  logits = Dense(1)(x)
  model = Model(inputs=inputs, outputs=logits, name="model")

  # check that pre-trained model is initialized
  # NOTE: This occurs because using pretrained weights ends up calling `K.batch_set_value`, which
  # creates assignment ops and calls `K.get_session()` to get the session and then run the
  # assignment ops.  The `K.get_session()` call initializes the model variables to random values
  # and sets the `_keras_initialized` attribute to True for each variable.  Then the assignment ops
  # run and actually set the variables to the pretrained values.  Without pretrained weights, the
  # `K.get_session()` function is not called upon model creation, and thus these variables will
  # remain uninitialized.  Furthermore, if we set `K.manual_variable_initialization(True)`, the
  # pretrained weights will be loaded, but there will be no indication that those variables were
  # already initialized, and thus we will end up reinitializing them to random values.  This is all
  # a byproduct of using Keras + TensorFlow in a hybrid setup, and we should look into making this
  # less brittle.
  for v in model_base.weights:
    assert hasattr(v, '_keras_initialized') and v._keras_initialized  # check for initialization
    assert sess.run(tf.is_variable_initialized(v))  # check for initialization

  # the new dense layer is not initialized yet
  #with pytest.raises(AssertionError):
  assert len(model.layers[-1].weights) == 2
  for v in model.layers[-1].weights:
    assert not getattr(v, '_keras_initialized', False)
    assert not sess.run(tf.is_variable_initialized(v))

  # initialize variables, including marking them with the `_keras_initialized` attribute
  initialize_variables(sess)

  # check that everything is initialized and marked with the `_keras_initialized` attribute
  # NOTE: this is important for a hybrid Keras & TensorFlow setup where Keras is being used for the
  # model creation part, and raw TensorFlow is being used for the rest.  if variables are not
  # initialized *and* marked with the special Keras attribute, then certain Keras functions will end
  # up accidentally reinitializing variables when they use `K.get_session()` internally.  In a pure
  # Keras setup, this would not happen since the model would be initialized at the proper times.  In
  # a Keras & TensorFlow hybrid setup, this can cause issues.  By encapsulating this nonsense in a
  # function, we can avoid these problems.
  for v in tf.global_variables():
    assert hasattr(v, '_keras_initialized') and v._keras_initialized  # check for initialization
    assert sess.run(tf.is_variable_initialized(v))  # check for initialization


def test_create_augmented_batch():
  import pytest

  reset()
  sess = K.get_session()

  image = np.random.rand(64,64,3)

  # wrong sizes
  with pytest.raises(AssertionError):
    create_augmented_batch(image, 3)
    create_augmented_batch(image, 31)

  # correct sizes
  def test(batch_size):
    aug_images_tf = create_augmented_batch(image, batch_size)
    aug_images = sess.run(aug_images_tf)
    assert aug_images.shape == (batch_size,64,64,3)

  test(32)
  test(4)
  test(1)

  # deterministic behavior
  def test2(batch_size):
    aug_images_1_tf = create_augmented_batch(image, batch_size)
    aug_images_1 = sess.run(aug_images_1_tf)

    aug_images_2_tf = create_augmented_batch(image, batch_size)
    aug_images_2 = sess.run(aug_images_2_tf)

    assert np.array_equal(aug_images_1, aug_images_2)

  test2(32)
  test2(4)
  test2(1)


def test_compute_l2_reg():
  reset()

  # create model with a mix of pretrained and new weights
  # NOTE: the pretrained layers will be initialized by Keras on creation, while the new Dense
  # layer will remain uninitialized
  input_shape = (224,224,3)
  inputs = Input(shape=input_shape)
  x = Dense(1)(inputs)
  logits = Dense(1)(x)
  model = Model(inputs=inputs, outputs=logits, name="model")

  for l in model.layers:
    l.trainable = True

  sess = K.get_session()

  # all layers
  l2_reg = compute_l2_reg(model)
  correct_l2_reg = tf.nn.l2_loss(model.layers[1].kernel) + tf.nn.l2_loss(model.layers[2].kernel)
  l2_reg_val, correct_l2_reg_val = sess.run([l2_reg, correct_l2_reg])
  assert np.array_equal(l2_reg_val, correct_l2_reg_val)

  # subset of layers
  model.layers[1].trainable = False
  l2_reg = compute_l2_reg(model)
  correct_l2_reg = tf.nn.l2_loss(model.layers[2].kernel)
  l2_reg_val, correct_l2_reg_val = sess.run([l2_reg, correct_l2_reg])
  assert np.array_equal(l2_reg_val, correct_l2_reg_val)

