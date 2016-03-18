#!/usr/bin/env python

import argparse, time
import numpy as np
import tensorflow as tf
from termcolor import colored
import regex as re

from image_encoder import ImageEncoder
from decoder import Decoder
from vocabulary import Vocabulary
from learning_utils import log, training_loop, print_args, print_title


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Trains the image captioning.')
    parser.add_argument("--train-images", type=argparse.FileType('rb'),
                        help="File with training images features", required=True)
    parser.add_argument("--valid-images", type=argparse.FileType('rb'),
                        help="File with validation images features.", required=True)
    parser.add_argument("--tokenized-train-text", type=argparse.FileType('r'),
                        help="File with tokenized training target sentences.", required=True)
    parser.add_argument("--tokenized-valid-text", type=argparse.FileType('r'), required=True)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--maximum-output", type=int, default=20)
    parser.add_argument("--use-attention", type=bool, default=False)
    parser.add_argument("--embeddings-size", type=int, default=256)
    parser.add_argument("--scheduled-sampling", type=float, default=None)
    parser.add_argument("--dropout-keep-prob", type=float, default=1.0)
    parser.add_argument("--l2-regularization", type=float, default=0.0)
    parser.add_argument("--epochs", type=int, default=10)
    args = parser.parse_args()

    print_title("IMAGE CAPTIONING ONLY")
    print_args(args)

    log("The training script started")
    training_images = np.load(args.train_images)
    args.train_images.close()
    log("Loaded training images.")
    validation_images = np.load(args.valid_images)
    args.valid_images.close()
    log("Loaded validation images.")

    training_sentences = [re.split(ur"[ @#]", l.rstrip()) for l in args.tokenized_train_text][:len(training_images)]
    log("Loaded {} training sentences.".format(len(training_sentences)))
    validation_sentences = [re.split(ur"[ @#]", l.rstrip()) for l in args.tokenized_valid_text][:len(validation_images)]
    validation_l = [[s] for s in validation_sentences]
    log("Loaded {} validation sentences.".format(len(validation_sentences)))

    vocabulary = \
        Vocabulary(tokenized_text=[w for s in training_sentences for w in s])

    log("Training vocabulary has {} words".format(len(vocabulary)))

    log("Buiding the TensorFlow computation graph.")
    dropout_placeholder = tf.placeholder(tf.float32, name="dropout_keep_prob")
    encoder = ImageEncoder(dropout_placeholder=dropout_placeholder)
    decoder = Decoder(encoder, vocabulary, embedding_size=args.embeddings_size,
            use_attention=args.use_attention, max_out_len=args.maximum_output, use_peepholes=True,
            scheduled_sampling=args.scheduled_sampling, dropout_placeholder=dropout_placeholder)

    def feed_dict(images, sentences, train=False):
        fd = {encoder.image_features: images}
        sentnces_tensors, weights_tensors = \
            vocabulary.sentences_to_tensor(sentences, args.maximum_output, train=train)
        for weight_plc, weight_tensor in zip(decoder.weights_ins, weights_tensors):
            fd[weight_plc] = weight_tensor

        for words_plc, words_tensor in zip(decoder.gt_inputs, sentnces_tensors):
            fd[words_plc] = words_tensor

        if train:
            fd[dropout_placeholder] = args.dropout_keep_prob
        else:
            fd[dropout_placeholder] = 1.0

        return fd

    valid_feed_dict = feed_dict(validation_images, validation_sentences)
    if args.l2_regularization > 0:
        with tf.variable_scope("l2_regularization"):
            l2_cost = args.l2_regularization * \
                sum([tf.reduce_sum(v ** 2) for v in tf.trainable_variables()])
    else:
        l2_cost = 0.0

    optimize_op = tf.train.AdamOptimizer().minimize(decoder.cost + l2_cost, global_step=decoder.learning_step)
    # gradients = optimizer.compute_gradients(cost)

    summary_train = tf.merge_summary(tf.get_collection("summary_train"))
    summary_test = tf.merge_summary(tf.get_collection("summary_test"))

    log("Initializing the TensorFlow session.")
    sess = tf.Session(config=tf.ConfigProto(inter_op_parallelism_threads=4,
                                            intra_op_parallelism_threads=4))
    sess.run(tf.initialize_all_variables())

    batched_training_sentenes = \
            [training_sentences[start:start + args.batch_size] \
             for start in range(0, len(training_sentences), args.batch_size)]
    batched_train_images = [training_images[start:start + args.batch_size]
             for start in range(0, len(training_sentences), args.batch_size)]
    training_feed_dicts = [feed_dict(imgs, sents) \
            for imgs, sents in zip(batched_train_images, batched_training_sentenes)]

    training_loop(sess, vocabulary, args.epochs, optimize_op, decoder,
                  training_feed_dicts, batched_training_sentenes,
                  valid_feed_dict, validation_l)