# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains a collection of models which operate on variable-length sequences.
"""
import math

import models
import video_level_models
import tensorflow as tf
import model_utils as utils

import tensorflow.contrib.slim as slim
from tensorflow import flags

FLAGS = flags.FLAGS
flags.DEFINE_integer("iterations", 30,
                     "Number of frames per batch for DBoF.")
flags.DEFINE_bool("dbof_add_batch_norm", True,
                  "Adds batch normalization to the DBoF model.")
flags.DEFINE_bool(
    "sample_random_frames", True,
    "If true samples random frames (for frame level models). If false, a random"
    "sequence of frames is sampled instead.")
flags.DEFINE_integer("dbof_cluster_size", 8192,
                     "Number of units in the DBoF cluster layer.")
flags.DEFINE_integer("dbof_hidden_size", 1024,
                     "Number of units in the DBoF hidden layer.")
flags.DEFINE_string("dbof_pooling_method", "max",
                    "The pooling method used in the DBoF cluster layer. "
                    "Choices are 'average' and 'max'.")
flags.DEFINE_string("video_level_classifier_model", "MoeModel",
                    "Some Frame-Level models can be decomposed into a "
                    "generalized pooling operation followed by a "
                    "classifier layer")

flags.DEFINE_integer("lstm_cells", 512, "Number of LSTM cells.")
flags.DEFINE_integer("lstm_layers", 1, "Number of LSTM layers.")
flags.DEFINE_bool("use_lstm_output", False, 
                  "Use LSTM output instead of state for classification")
flags.DEFINE_string("pooling_method", "average",
                    "The type of pooling of frame level features to use.")

flags.DEFINE_integer("num_filters", 32, "Number of 1D convolution filters")
flags.DEFINE_integer("filter_size", 5, "size of the 1D convolution filters")

flags.DEFINE_integer("time_skip", 2, "Number of time skips in each layer")

flags.DEFINE_integer("pool_size", 3, "The time frame to pool over")
flags.DEFINE_integer("pool_stride", 1, "The stride over which to perform time frame pooling")
flags.DEFINE_string("pool_type", "AVG", "The type of pooling to use in between LSTM layers")
flags.DEFINE_bool("learned_pooling", False, "Whether to have a learnable pooling operation")

class FrameLevelLogisticModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a logistic classifier over the average of the
    frame-level features.

    This class is intended to be an example for implementors of frame level
    models. If you want to train a model over averaged features it is more
    efficient to average them beforehand rather than on the fly.

    Args:
      model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                   input features.
      vocab_size: The number of classes in the dataset.
      num_frames: A vector of length 'batch' which indicates the number of
           frames for each video (before padding).

    Returns:
      A dictionary with a tensor containing the probability predictions of the
      model in the 'predictions' key. The dimensions of the tensor are
      'batch_size' x 'num_classes'.
    """
    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    feature_size = model_input.get_shape().as_list()[2]

    denominators = tf.reshape(
        tf.tile(num_frames, [1, feature_size]), [-1, feature_size])
    avg_pooled = tf.reduce_sum(model_input,
                               axis=[1]) / denominators

    output = slim.fully_connected(
        avg_pooled, vocab_size, activation_fn=tf.nn.sigmoid,
        weights_regularizer=slim.l2_regularizer(1e-8))
    return {"predictions": output}

class DbofModel(models.BaseModel):
  """Creates a Deep Bag of Frames model.

  The model projects the features for each frame into a higher dimensional
  'clustering' space, pools across frames in that space, and then
  uses a configurable video-level model to classify the now aggregated features.

  The model will randomly sample either frames or sequences of frames during
  training to speed up convergence.

  Args:
    model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                 input features.
    vocab_size: The number of classes in the dataset.
    num_frames: A vector of length 'batch' which indicates the number of
         frames for each video (before padding).

  Returns:
    A dictionary with a tensor containing the probability predictions of the
    model in the 'predictions' key. The dimensions of the tensor are
    'batch_size' x 'num_classes'.
  """

  def create_model(self,
                   model_input,
                   vocab_size,
                   num_frames,
                   iterations=None,
                   add_batch_norm=None,
                   sample_random_frames=None,
                   cluster_size=None,
                   hidden_size=None,
                   is_training=True,
                   **unused_params):
    iterations = iterations or FLAGS.iterations
    add_batch_norm = add_batch_norm or FLAGS.dbof_add_batch_norm
    random_frames = sample_random_frames or FLAGS.sample_random_frames
    cluster_size = cluster_size or FLAGS.dbof_cluster_size
    hidden1_size = hidden_size or FLAGS.dbof_hidden_size

    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    if random_frames:
      model_input = utils.SampleRandomFrames(model_input, num_frames,
                                             iterations)
    else:
      model_input = utils.SampleRandomSequence(model_input, num_frames,
                                               iterations)
    max_frames = model_input.get_shape().as_list()[1]
    feature_size = model_input.get_shape().as_list()[2]
    reshaped_input = tf.reshape(model_input, [-1, feature_size])
    tf.summary.histogram("input_hist", reshaped_input)

    if add_batch_norm:
      reshaped_input = slim.batch_norm(
          reshaped_input,
          center=True,
          scale=True,
          is_training=is_training,
          scope="input_bn")

    cluster_weights = tf.get_variable("cluster_weights",
      [feature_size, cluster_size],
      initializer = tf.random_normal_initializer(stddev=1 / math.sqrt(feature_size)))
    tf.summary.histogram("cluster_weights", cluster_weights)
    activation = tf.matmul(reshaped_input, cluster_weights)
    if add_batch_norm:
      activation = slim.batch_norm(
          activation,
          center=True,
          scale=True,
          is_training=is_training,
          scope="cluster_bn")
    else:
      cluster_biases = tf.get_variable("cluster_biases",
        [cluster_size],
        initializer = tf.random_normal(stddev=1 / math.sqrt(feature_size)))
      tf.summary.histogram("cluster_biases", cluster_biases)
      activation += cluster_biases
    activation = tf.nn.relu6(activation)
    tf.summary.histogram("cluster_output", activation)

    activation = tf.reshape(activation, [-1, max_frames, cluster_size])
    activation = utils.FramePooling(activation, FLAGS.dbof_pooling_method)

    hidden1_weights = tf.get_variable("hidden1_weights",
      [cluster_size, hidden1_size],
      initializer=tf.random_normal_initializer(stddev=1 / math.sqrt(cluster_size)))
    tf.summary.histogram("hidden1_weights", hidden1_weights)
    activation = tf.matmul(activation, hidden1_weights)
    if add_batch_norm:
      activation = slim.batch_norm(
          activation,
          center=True,
          scale=True,
          is_training=is_training,
          scope="hidden1_bn")
    else:
      hidden1_biases = tf.get_variable("hidden1_biases",
        [hidden1_size],
        initializer = tf.random_normal_initializer(stddev=0.01))
      tf.summary.histogram("hidden1_biases", hidden1_biases)
      activation += hidden1_biases
    activation = tf.nn.relu6(activation)
    tf.summary.histogram("hidden1_output", activation)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    return aggregated_model().create_model(
        model_input=activation,
        vocab_size=vocab_size,
        **unused_params)

class LstmModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a stack of LSTMs to represent the video.

    Args:
      model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                   input features.
      vocab_size: The number of classes in the dataset.
      num_frames: A vector of length 'batch' which indicates the number of
           frames for each video (before padding).

    Returns:
      A dictionary with a tensor containing the probability predictions of the
      model in the 'predictions' key. The dimensions of the tensor are
      'batch_size' x 'num_classes'.
    """
    lstm_size = FLAGS.lstm_cells
    number_of_layers = FLAGS.lstm_layers

    if FLAGS.use_attention:
      stacked_lstm = tf.contrib.rnn.MultiRNNCell(
              [
                tf.contrib.rnn.AttentionCellWrapper(
                    tf.contrib.rnn.BasicLSTMCell(
                      lstm_size, forget_bias=1.0), FLAGS.attention_len)
                for _ in range(number_of_layers)
              ])
    elif FLAGS.use_residuals:
      stacked_lstm = tf.contrib.rnn.MultiRNNCell(
              [
                tf.contrib.rnn.ResidualWrapper(
                    tf.contrib.rnn.BasicLSTMCell(
                      lstm_size, forget_bias=1.0))
                for _ in range(number_of_layers)
              ])
    else:
      stacked_lstm = tf.contrib.rnn.MultiRNNCell(
              [
                tf.contrib.rnn.BasicLSTMCell(
                      lstm_size, forget_bias=1.0)
                for _ in range(number_of_layers)
              ])

    loss = 0.0

    outputs, state = tf.nn.dynamic_rnn(stacked_lstm, model_input,
                                       sequence_length=num_frames,
                                       dtype=tf.float32)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)

    if FLAGS.use_lstm_output:
      agg_model_inputs = utils.FramePooling(outputs,FLAGS.pooling_method)
    else:
      agg_model_inputs = state[-1].h

    return aggregated_model().create_model(
          model_input=agg_model_inputs,
          vocab_size=vocab_size,
          **unused_params)

class BidirectionalLSTMModel(models.BaseModel):
  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    lstm_size = FLAGS.lstm_cells
    number_of_layers = FLAGS.lstm_layers
		
    lstm_fw = tf.contrib.rnn.MultiRNNCell(
					[tf.contrib.rnn.BasicLSTMCell(lstm_size)
					for _ in range(number_of_layers)
				], state_is_tuple=False)

    lstm_bw = tf.contrib.rnn.MultiRNNCell(
					[tf.contrib.rnn.BasicLSTMCell(lstm_size)
					for _ in range(number_of_layers)
				], state_is_tuple=False)
		
    loss = 0.0
    with tf.variable_scope("RNN"):
      outputs1,states1 = tf.nn.bidirectional_dynamic_rnn(lstm_fw,
                                    lstm_bw,
                                    model_input,
                                    dtype=tf.float32,
                                    sequence_length=num_frames)
    outputs = tf.concat(outputs1, 2)
    state = tf.concat(states1, 1)

    aggregated_model = getattr(video_level_models, FLAGS.video_level_classifier_model)

    if FLAGS.use_lstm_output:
      agg_model_inputs = utils.FramePooling(outputs,FLAGS.pooling_method)
    else:
      agg_model_inputs = state[-1].h
    
    return aggregated_model().create_model(
          model_input=agg_model_inputs,
          vocab_size=vocab_size,
          **unused_params)

class GRUModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a stack of LSTMs to represent the video.
    Args:
      model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                   input features.
      vocab_size: The number of classes in the dataset.
      num_frames: A vector of length 'batch' which indicates the number of
           frames for each video (before padding).
    Returns:
      A dictionary with a tensor containing the probability predictions of the
      model in the 'predictions' key. The dimensions of the tensor are
      'batch_size' x 'num_classes'.
    """
    gru_size = FLAGS.lstm_cells
    number_of_layers = FLAGS.lstm_layers

    ## Batch normalize the input
    if FLAGS.use_attention:
      stacked_gru = tf.contrib.rnn.MultiRNNCell(
              [
                tf.contrib.rnn.AttentionCellWrapper(
                  tf.contrib.rnn.GRUCell(gru_size), FLAGS.attention_len)
                for _ in range(number_of_layers)
              ])
    elif FLAGS.use_residuals:
      stacked_gru = tf.contrib.rnn.MultiRNNCell(
              [
                tf.contrib.rnn.ResidualWrapper(
                    tf.contrib.rnn.GRUCell(gru_size))
                for _ in range(number_of_layers)
              ])
    else:
      stacked_gru = tf.contrib.rnn.MultiRNNCell(
              [
                  tf.contrib.rnn.GRUCell(gru_size)
                  for _ in range(number_of_layers)
              ])

    loss = 0.0
    with tf.variable_scope("RNN"):
      outputs, state = tf.nn.dynamic_rnn(stacked_gru, model_input,
                                         sequence_length=num_frames,
                                         dtype=tf.float32)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    if FLAGS.use_lstm_output:
      agg_model_inputs = utils.FramePooling(outputs,FLAGS.pooling_method)
    else:
      agg_model_inputs = state[-1]
    
    return aggregated_model().create_model(
          model_input=agg_model_inputs,
          vocab_size=vocab_size,
          **unused_params)

def conv1D_pool(inputs,filter_size,stride,padding='VALID',name='pooling'):
  with tf.variable_scope(name):
    input_dims = inputs.get_shape().as_list()
    channels = input_dims[2]
    filters = tf.get_variable("pooling_weights",[filter_size,channels,channels],
                              initializer=tf.random_normal_initializer)
    out = tf.nn.conv1d(inputs,filters,stride=stride,padding=padding,name="learned_pooling")
  return out

class TemporalPoolingNetworkModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a stack of GRU's with temporal pooling
    """
    gru_size = FLAGS.lstm_cells
    pool_size = FLAGS.pool_size
    pool_stride = FLAGS.pool_stride
    pool_type = FLAGS.pool_type
    learned_pooling = FLAGS.learned_pooling

    with tf.variable_scope("rnn_1"):
      gru_1 = tf.contrib.rnn.GRUCell(gru_size)
      outputs, state = tf.nn.dynamic_rnn(gru_1, model_input,
                                         sequence_length=num_frames,
                                         dtype=tf.float32)

    if learned_pooling:
      pooled_outputs = conv1D_pool(outputs,pool_size,pool_stride)
      new_seq_lenth = (num_frames - pool_size)/pool_stride + 1
    else:
      pooled_outputs = tf.nn.pool(outputs, [pool_size], pool_type,
                                  "VALID", strides=[pool_stride])
      new_seq_lenth = (num_frames - pool_size)/pool_stride + 1

    with tf.variable_scope("rnn_2"):
      gru_2 = tf.contrib.rnn.GRUCell(gru_size)
      outputs2, state2 = tf.nn.dynamic_rnn(gru_2, pooled_outputs,
                                           sequence_length=new_seq_lenth,
                                           dtype=tf.float32)
    
    loss = 0.0

    model_state = tf.concat([state, state2], axis=1)
    model_outputs = tf.concat([outputs, outputs2], axis=1)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    
    if FLAGS.use_lstm_output:
      return aggregated_model().create_model(
        model_input=utils.FramePooling(model_outputs,FLAGS.pooling_method),
        vocab_size=vocab_size,
        **unused_params)
    else:
      return aggregated_model().create_model(
          model_input=model_state,
          vocab_size=vocab_size,
          **unused_params)

class TemporalSkippingNetworkModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a stack of GRU's with temporal pooling
    """
    gru_size = FLAGS.lstm_cells
    pool_type = FLAGS.pool_type
    time_skip = FLAGS.time_skip

    with tf.variable_scope("rnn_1"):
      gru_1 = tf.contrib.rnn.GRUCell(gru_size)
      outputs, state = tf.nn.dynamic_rnn(gru_1, model_input,
                                         sequence_length=num_frames,
                                         dtype=tf.float32)

    skipped_outputs = outputs[:,::time_skip,:]
    new_seq_length = num_frames / time_skip

    with tf.variable_scope("rnn_2"):
      gru_2 = tf.contrib.rnn.GRUCell(gru_size)
      outputs2, state2 = tf.nn.dynamic_rnn(gru_2, skipped_outputs,
                                           sequence_length=new_seq_lenth,
                                           dtype=tf.float32)
    
    loss = 0.0

    model_state = tf.concat([state, state2], axis=1)
    model_outputs = tf.concat([outputs, outputs2], axis=1)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    
    if FLAGS.use_lstm_output:
      return aggregated_model().create_model(
        model_input=utils.FramePooling(model_outputs,FLAGS.pooling_method),
        vocab_size=vocab_size,
        **unused_params)
    else:
      return aggregated_model().create_model(
          model_input=model_state,
          vocab_size=vocab_size,
          **unused_params)
