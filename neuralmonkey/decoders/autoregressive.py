# pylint: disable=too-many-lines
"""Abstract class for autoregressive decoding.

Either for the recurrent decoder, or for the transformer decoder.

The autoregressive decoder uses the while loop to get the outputs.
Descendants should only specify the initial state and the while loop body.
"""
from typing import NamedTuple, Callable, Optional, Any, List, Dict, Tuple

import tensorflow as tf

from neuralmonkey.dataset import Dataset
from neuralmonkey.decorators import tensor
from neuralmonkey.model.feedable import FeedDict
from neuralmonkey.model.parameterized import InitializerSpecs
from neuralmonkey.model.model_part import ModelPart
from neuralmonkey.logging import warn
from neuralmonkey.model.sequence import EmbeddedSequence
from neuralmonkey.nn.utils import dropout
from neuralmonkey.tf_utils import (
    append_tensor, get_variable, get_state_shape_invariants)
from neuralmonkey.vocabulary import (
    Vocabulary, pad_batch, sentence_mask, UNK_TOKEN_INDEX, START_TOKEN_INDEX,
    END_TOKEN_INDEX, PAD_TOKEN_INDEX)


class LoopState(NamedTuple(
        "LoopState",
        [("histories", Any),
         ("constants", Any),
         ("feedables", Any)])):
    """The loop state object.

    The LoopState is a structure that works with the tf.while_loop function the
    decoder loop state stores all the information that is not invariant for the
    decoder run.

    Attributes:
        histories: A set of tensors that grow in time as the decoder proceeds.
        constants: A set of independent tensors that do not change during the
            entire decoder run.
        feedables: A set of tensors used as the input of a single decoder step.
    """


class DecoderHistories(NamedTuple(
        "DecoderHistories",
        [("logits", tf.Tensor),
         ("output_states", tf.Tensor),
         ("output_symbols", tf.Tensor),
         ("output_mask", tf.Tensor),
         ("other", Any)])):
    """The values collected during the run of an autoregressive decoder.

    This should only record decoding history and the decoding should not be
    dependent on these values.

    Attributes defined here (and in the `other`) substructure should always
    be time-major (e.g., shape(time, batch, ...)).

    Attributes:
        logits: A tensor of shape ``(time, batch, vocabulary)`` which contains
            the unnormalized output scores of words in a vocabulary.
        output_states: A tensor of shape ``(time, batch, state_size)``. The
            states of the decoder before the final output (logit) projection.
        output_symbols: An int tensor of shape ``(time, batch)``. Stores the
            generated symbols. (Either an argmax-ed value from the logits, or
            a target token, during training.)
        output_mask: A float tensor of zeros and ones of shape
            ``(time, batch)``. Keeps track of valid positions in the decoded
            data.
        other: A structure related to a specific AutoregressiveDecoder
            implementation.
    """


class DecoderConstants(NamedTuple(
        "DecoderConstants",
        [("train_inputs", Optional[tf.Tensor])])):
    """The constants used by an autoregressive decoder.

    Attributes:
        train_inputs: During training, this is populated by the target token
            ids.
    """


class DecoderFeedables(NamedTuple(
        "DecoderFeedables",
        [("step", tf.Tensor),
         ("finished", tf.Tensor),
         ("embedded_input", tf.Tensor),
         ("other", Any)])):
    """The input of a single step of an autoregressive decoder.

    The decoder should be able to generate an output symbol only using the
    information contained in this structure.

    Attributes defined here (and in the `other`) substructure should always
    be batch-major (e.g., shape(batch, ...)).

    Attributes:
        step: A scalar int tensor, stores the number of the current time step.
        finished: A boolean tensor of shape ``(batch)``,  which says whether
            the decoding of a sentence in the batch is finished or not. (E.g.
            whether the end token has already been generated.)
        embedded_input: A ``batch``-sized tensor with embedded inputs to the
            decoder. During inference, this contains the previously generated
            tokens. During training, this contains the reference tokens.
        other: A structure related to a specific AutoregressiveDecoder
            implementation.
    """


# pylint: disable=too-many-public-methods,too-many-instance-attributes
class AutoregressiveDecoder(ModelPart):

    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(self,
                 name: str,
                 vocabulary: Vocabulary,
                 data_id: str,
                 max_output_len: int,
                 dropout_keep_prob: float = 1.0,
                 embedding_size: int = None,
                 embeddings_source: EmbeddedSequence = None,
                 tie_embeddings: bool = False,
                 label_smoothing: float = None,
                 supress_unk: bool = False,
                 reuse: ModelPart = None,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None,
                 initializers: InitializerSpecs = None) -> None:
        """Initialize parameters common for all autoregressive decoders.

        Arguments:
            name: Name of the decoder. Should be unique accross all Neural
                Monkey objects.
            vocabulary: Target vocabulary.
            data_id: Target data series.
            max_output_len: Maximum length of an output sequence.
            reuse: Reuse the variables from the model part.
            dropout_keep_prob: Probability of keeping a value during dropout.
            embedding_size: Size of embedding vectors for target words.
            embeddings_source: Embedded sequence to take embeddings from.
            tie_embeddings: Use decoder.embedding_matrix also in place
                of the output decoding matrix.
            label_smoothing: Label smoothing parameter.
            supress_unk: If true, decoder will not produce symbols for unknown
                tokens.
        """
        ModelPart.__init__(self, name, reuse, save_checkpoint, load_checkpoint,
                           initializers)

        self.vocabulary = vocabulary
        self.data_id = data_id
        self.max_output_len = max_output_len
        self.dropout_keep_prob = dropout_keep_prob
        self._embedding_size = embedding_size
        self.embeddings_source = embeddings_source
        self.label_smoothing = label_smoothing
        self.tie_embeddings = tie_embeddings
        self.supress_unk = supress_unk

        self.encoder_states = lambda: []  # type: Callable[[], List[tf.Tensor]]
        self.encoder_masks = lambda: []  # type: Callable[[], List[tf.Tensor]]

        # Check the values of the parameters (max_output_len, ...)
        if self.max_output_len <= 0:
            raise ValueError(
                "Maximum sequence length must be a positive integer.")

        if self._embedding_size is not None and self._embedding_size <= 0:
            raise ValueError("Embedding size must be a positive integer.")

        if self.dropout_keep_prob < 0.0 or self.dropout_keep_prob > 1.0:
            raise ValueError("Dropout keep probability must be a real number "
                             "in the interval [0,1].")
    # pylint: enable=too-many-arguments,too-many-locals

    @property
    def embedding_size(self) -> int:
        if self.embeddings_source is None:
            if self._embedding_size is None:
                raise ValueError(
                    "You must specify either embedding size or the embedded "
                    "sequence from which to reuse the embeddings (e.g. set "
                    "'embedding_size' or 'embeddings_source' parameter)")
            return self._embedding_size

        if self.embeddings_source is not None:
            if self._embedding_size is not None:
                warn("Overriding the embedding_size parameter with the "
                     "size of the reused embeddings from the encoder.")

        return self.embeddings_source.embedding_matrix.get_shape()[1].value

    @tensor
    def go_symbols(self) -> tf.Tensor:
        return tf.fill([self.batch_size],
                       tf.constant(START_TOKEN_INDEX, dtype=tf.int64))

    @property
    def input_types(self) -> Dict[str, tf.DType]:
        return {self.data_id: tf.string}

    @property
    def input_shapes(self) -> Dict[str, tf.TensorShape]:
        return {self.data_id: tf.TensorShape([None, None])}

    @tensor
    def train_tokens(self) -> tf.Tensor:
        return self.dataset[self.data_id]

    @tensor
    def train_inputs(self) -> tf.Tensor:
        return tf.transpose(
            self.vocabulary.strings_to_indices(self.train_tokens))

    @tensor
    def train_mask(self) -> tf.Tensor:
        return sentence_mask(self.train_inputs)

    @tensor
    def decoding_w(self) -> tf.Variable:
        if (self.tie_embeddings
                and self.embedding_size != self.output_dimension):
            raise ValueError(
                "`embedding_size must be equal to the output_projection "
                "size when using the `tie_embeddings` option")

        with tf.name_scope("output_projection"):
            if self.tie_embeddings:
                return tf.transpose(self.embedding_matrix)

            return get_variable(
                "state_to_word_W",
                [self.output_dimension, len(self.vocabulary)],
                initializer=tf.random_uniform_initializer(-0.5, 0.5))

    @tensor
    def decoding_b(self) -> Optional[tf.Variable]:
        if self.tie_embeddings:
            return tf.zeros(len(self.vocabulary))

        with tf.name_scope("output_projection"):
            return get_variable(
                "state_to_word_b",
                [len(self.vocabulary)],
                initializer=tf.zeros_initializer())

    @tensor
    def embedding_matrix(self) -> tf.Variable:
        """Variables and operations for embedding of input words.

        If we are reusing word embeddings, this function takes the embedding
        matrix from the first encoder
        """
        if self.embeddings_source is not None:
            return self.embeddings_source.embedding_matrix

        assert self.embedding_size is not None

        return get_variable(
            name="word_embeddings",
            shape=[len(self.vocabulary), self.embedding_size])

    def embed_input_symbols(self, input_symbols: tf.Tensor) -> tf.Tensor:
        embedded_input = tf.nn.embedding_lookup(
            self.embedding_matrix, input_symbols)
        return dropout(embedded_input, self.dropout_keep_prob, self.train_mode)

    @tensor
    def train_loop_result(self) -> LoopState:
        return self.decoding_loop(train_mode=True)

    @tensor
    def train_logits(self) -> tf.Tensor:
        train_result = LoopState(*self.train_loop_result)
        return train_result.histories.logits

    @tensor
    def train_output_states(self) -> tf.Tensor:
        train_result = LoopState(*self.train_loop_result)
        return train_result.histories.output_states

    @tensor
    def train_logprobs(self) -> tf.Tensor:
        return tf.nn.log_softmax(self.train_logits)

    @tensor
    def train_xents(self) -> tf.Tensor:
        train_targets = tf.transpose(self.train_inputs)
        softmax_function = None
        if self.label_smoothing:
            softmax_function = (
                lambda labels, logits: tf.losses.softmax_cross_entropy(
                    tf.one_hot(labels, len(self.vocabulary)),
                    logits, label_smoothing=self.label_smoothing))

        # Return losses of shape (batch, time). Losses on invalid positions
        # are zero.
        return tf.contrib.seq2seq.sequence_loss(
            tf.transpose(self.train_logits, perm=[1, 0, 2]),
            train_targets,
            tf.transpose(self.train_mask),
            average_across_batch=False,
            average_across_timesteps=False,
            softmax_loss_function=softmax_function)

    @tensor
    def train_loss(self) -> tf.Tensor:
        # Cross entropy mean over all words in the batch
        # (could also be done as a mean over sentences)
        return tf.reduce_sum(self.train_xents) / tf.reduce_sum(self.train_mask)

    @property
    def cost(self) -> tf.Tensor:
        return self.train_loss

    @tensor
    def runtime_loop_result(self) -> LoopState:
        return self.decoding_loop(train_mode=False)

    @tensor
    def runtime_logits(self) -> tf.Tensor:
        runtime_result = LoopState(*self.runtime_loop_result)
        return runtime_result.histories.logits

    @tensor
    def runtime_output_states(self) -> tf.Tensor:
        runtime_result = LoopState(*self.runtime_loop_result)
        return runtime_result.histories.output_states

    @tensor
    def runtime_mask(self) -> tf.Tensor:
        runtime_result = LoopState(*self.runtime_loop_result)
        return runtime_result.histories.output_mask

    @tensor
    def decoded(self) -> tf.Tensor:
        # We disable generating of <pad> tokens at index 0
        # (self.runtime_logits[:, :, 1:]). This shifts the indices
        # of the decoded tokens (therefore, we add +1 to the decoded
        # output indices).

        # self.runtime_logits is of size [batch, sentence_len, vocabulary_size]
        return tf.argmax(self.runtime_logits[:, :, 1:], -1) + 1

    @tensor
    def runtime_xents(self) -> tf.Tensor:
        train_targets = tf.transpose(self.train_inputs)
        batch_major_logits = tf.transpose(self.runtime_logits, [1, 0, 2])
        min_time = tf.minimum(tf.shape(train_targets)[1],
                              tf.shape(batch_major_logits)[1])

        # NOTE if done properly, there should be padding of the shorter
        # sequence instead of cropping to the length of the shorter one

        return tf.contrib.seq2seq.sequence_loss(
            logits=batch_major_logits[:, :min_time],
            targets=train_targets[:, :min_time],
            weights=tf.transpose(self.train_mask)[:, :min_time],
            average_across_batch=False,
            average_across_timesteps=False)

    @tensor
    def runtime_loss(self) -> tf.Tensor:
        return (tf.reduce_sum(self.runtime_xents)
                / tf.reduce_sum(tf.to_float(self.runtime_mask)))

    @tensor
    def runtime_logprobs(self) -> tf.Tensor:
        return tf.nn.log_softmax(self.runtime_logits)

    @property
    def output_dimension(self) -> int:
        raise NotImplementedError("Abstract property")

    def get_initial_feedables(self) -> DecoderFeedables:
        return DecoderFeedables(
            step=tf.constant(0, tf.int32),
            finished=tf.zeros([self.batch_size], dtype=tf.bool),
            embedded_input=self.embed_input_symbols(self.go_symbols),
            other=None)

    def get_initial_histories(self) -> DecoderHistories:
        output_states = tf.zeros(
            shape=[0, self.batch_size, self.embedding_size],
            dtype=tf.float32,
            name="hist_output_states")

        output_mask = tf.zeros(
            shape=[0, self.batch_size],
            dtype=tf.bool,
            name="hist_output_mask")

        output_symbols = tf.zeros(
            shape=[0, self.batch_size],
            dtype=tf.int64,
            name="hist_output_symbols")

        logits = tf.zeros(
            shape=[0, self.batch_size, len(self.vocabulary)],
            dtype=tf.float32,
            name="hist_logits")

        return DecoderHistories(
            logits=logits,
            output_states=output_states,
            output_mask=output_mask,
            output_symbols=output_symbols,
            other=None)

    def get_initial_constants(self) -> DecoderConstants:
        return DecoderConstants(train_inputs=self.train_inputs)

    def get_initial_loop_state(self) -> LoopState:
        return LoopState(
            feedables=self.get_initial_feedables(),
            histories=self.get_initial_histories(),
            constants=self.get_initial_constants())

    def loop_continue_criterion(self, *args) -> tf.Tensor:
        """Decide whether to break out of the while loop.

        Arguments:
            loop_state: ``LoopState`` instance (see the docs for this module).
                Represents current decoder loop state.
        """
        loop_state = LoopState(*args)
        finished = loop_state.feedables.finished
        not_all_done = tf.logical_not(tf.reduce_all(finished))
        before_max_len = tf.less(loop_state.feedables.step,
                                 self.max_output_len)
        return tf.logical_and(not_all_done, before_max_len)

    def next_state(self, loop_state: LoopState) -> Tuple[tf.Tensor, Any, Any]:
        raise NotImplementedError("Abstract method.")

    def get_body(self, train_mode: bool, sample: bool = False,
                 temperature: float = 1.) -> Callable:
        """Return the while loop body function."""

        def is_finished(finished: tf.Tensor, symbols: tf.Tensor) -> tf.Tensor:
            has_just_finished = tf.equal(symbols, END_TOKEN_INDEX)
            return tf.logical_or(finished, has_just_finished)

        def state_to_logits(state: tf.Tensor) -> tf.Tensor:
            logits = tf.matmul(state, self.decoding_w)
            logits += self.decoding_b

            if self.supress_unk:
                unk_mask = tf.one_hot(
                    UNK_TOKEN_INDEX, depth=len(self.vocabulary), on_value=-1e9)
                logits += unk_mask

            return logits

        def logits_to_symbols(logits: tf.Tensor,
                              loop_state: LoopState) -> tf.Tensor:
            step = loop_state.feedables.step
            if sample:
                next_symbols = tf.squeeze(
                    tf.multinomial(logits, num_samples=1), axis=1)
            elif train_mode:
                next_symbols = loop_state.constants.train_inputs[step]
            else:
                next_symbols = tf.argmax(logits, axis=1)

            int_unfinished_mask = tf.to_int64(
                tf.logical_not(loop_state.feedables.finished))

            # Note this works only when PAD_TOKEN_INDEX is 0. Otherwise
            # this have to be rewritten
            assert PAD_TOKEN_INDEX == 0
            next_symbols = next_symbols * int_unfinished_mask

            return next_symbols

        def body(*args) -> LoopState:

            loop_state = LoopState(*args)
            feedables = loop_state.feedables
            histories = loop_state.histories

            with tf.variable_scope(self._variable_scope, reuse=tf.AUTO_REUSE):
                output_state, dec_other, hist_other = self.next_state(
                    loop_state)

                logits = state_to_logits(output_state)
                logits /= temperature

                next_symbols = logits_to_symbols(logits, loop_state)
                finished = is_finished(feedables.finished, next_symbols)

            next_feedables = DecoderFeedables(
                step=feedables.step + 1,
                finished=finished,
                embedded_input=self.embed_input_symbols(next_symbols),
                other=dec_other)

            next_histories = DecoderHistories(
                logits=append_tensor(histories.logits, logits),
                output_states=append_tensor(
                    histories.output_states, output_state),
                output_symbols=append_tensor(
                    histories.output_symbols, next_symbols),
                output_mask=append_tensor(
                    histories.output_mask, tf.logical_not(finished)),
                other=hist_other)

            return LoopState(
                feedables=next_feedables,
                histories=next_histories,
                constants=loop_state.constants)

        return body

    def finalize_loop(self, final_loop_state: LoopState,
                      train_mode: bool) -> None:
        """Execute post-while loop operations.

        Arguments:
            final_loop_state: Decoder loop state at the end
                of the decoding loop.
            train_mode: Boolean flag, telling whether this is
                a training run.
        """

    def decoding_loop(self, train_mode: bool, sample: bool = False,
                      temperature: float = 1) -> LoopState:
        """Run the decoding while loop.

        Calls get_initial_loop_state and constructs tf.while_loop
        with the continuation criterion returned from loop_continue_criterion,
        and body function returned from get_body.

        After finishing the tf.while_loop, it calls finalize_loop
        to further postprocess the final decoder loop state (usually
        by stacking Tensors containing decoding histories).

        Arguments:
            train_mode: Boolean flag, telling whether this is
                a training run.
            sample: Boolean flag, telling whether we should sample
                the output symbols from the output distribution instead
                of using argmax or gold data.
            temperature: float value specifying the softmax temperature
        """
        initial_loop_state = self.get_initial_loop_state()
        with tf.control_dependencies([self.decoding_w, self.decoding_b]):
            final_loop_state = tf.while_loop(
                self.loop_continue_criterion,
                self.get_body(train_mode, sample, temperature),
                initial_loop_state,
                shape_invariants=tf.contrib.framework.nest.map_structure(
                    get_state_shape_invariants, initial_loop_state))
        self.finalize_loop(final_loop_state, train_mode)

        return final_loop_state

    def feed_dict(self, dataset: Dataset, train: bool = False) -> FeedDict:
        """Populate the feed dictionary for the decoder object.

        Arguments:
            dataset: The dataset to use for the decoder.
            train: Boolean flag, telling whether this is a training run.
        """
        fd = ModelPart.feed_dict(self, dataset, train)

        sentences = dataset.maybe_get_series(self.data_id)

        if sentences is None and train:
            raise ValueError("When training, you must feed "
                             "reference sentences")

        if sentences is not None:
            fd[self.train_tokens] = pad_batch(
                list(sentences), self.max_output_len, add_start_symbol=False,
                add_end_symbol=True)

        return fd
