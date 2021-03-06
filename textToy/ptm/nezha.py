# -*- coding: UTF-8 -*-
"""
@author: huanghui
@file: nezha.py
@date: 2020/09/08
"""
from .embedding import (word_embeddings_layer,
                        token_type_embedding_layer)
from .transformer import bert_transformer_layer, pooler_layer, dense_layer_2d
from .utils import (reshape_from_matrix,
                    reshape_to_matrix,
                    get_shape_list,
                    layer_norm_and_dropout,
                    create_attention_mask_from_input_mask,
                    )
from .utils import get_activation
import tensorflow.compat.v1 as tf
from .base import BaseModel


def NeZhaEmbedding(
        config,
        input_ids,
        token_type_ids=None
):
    '''
    nezha模型的embedding部分，nezha和bert在此的区别是没有使用绝对位置embedding
    :param config: bert配置文件
    :param input_ids: word ids
    :param token_type_ids:
    :return:
    '''
    (embedding_output, embedding_table) = word_embeddings_layer(
        input_ids=input_ids,
        vocab_size=config.vocab_size,
        embedding_size=config.hidden_size,
        initializer_range=config.initializer_range,
        word_embedding_name="word_embeddings",
        use_one_hot_embeddings=config.use_one_hot_embeddings
    )

    embedding_shape = get_shape_list(embedding_output, expected_rank=3)

    # Add positional embeddings and token type embeddings, then layer
    # normalize and perform dropout.

    if token_type_ids is None:
        raise ValueError("`token_type_ids` must be specified if"
                         "`use_token_type` is True.")
    token_type_embeddings = token_type_embedding_layer(
        input_shape=embedding_shape,
        token_type_ids=token_type_ids,
        token_type_vocab_size=config.type_vocab_size,
        token_type_embedding_name="token_type_embeddings",
        initializer_range=config.initializer_range
    )
    embedding_output += token_type_embeddings

    embedding_output = layer_norm_and_dropout(
        embedding_output,
        config.hidden_dropout_prob
    )
    return embedding_output, embedding_table


def NeZhaEncoder(
        config,
        input_tensor,
        attention_mask=None
):
    """
    华为nezha模型的编码器部分，nezha和bert的编码器区别就是attention部分增加相对位置信息
    :param config:
    :param input_tensor:
    :param attention_mask:
    :return:
    """

    # 隐藏层大小必须是头数的整倍数
    if config.hidden_size % config.num_attention_heads != 0:
        raise ValueError(
            "The hidden size (%d) is not a multiple of the number of attention "
            "heads (%d)" % (config.hidden_size, config.num_attention_heads))

    # input_tensor 一般是embedding，这里得到embedding 的shape，
    # embedding就是transformer的第一层输入
    input_shape = get_shape_list(input_tensor, expected_rank=3)
    batch_size = input_shape[0]
    seq_length = input_shape[1]
    input_width = input_shape[2]

    # The Transformer performs sum residuals on all layers so the input needs
    # to be the same as the hidden size.
    if input_width != config.hidden_size:
        input_tensor = dense_layer_2d(
            input_tensor, config.hidden_size, config.initializer_range,
            None, use_einsum=True, name="embedding_hidden_mapping_in"
        )

    # We keep the representation as a 2D tensor to avoid re-shaping it back and
    # forth from a 3D tensor to a 2D tensor. Re-shapes are normally free on
    # the GPU/CPU but may not be free on the TPU, so we want to minimize them to
    # help the optimizer.
    prev_output = reshape_to_matrix(input_tensor)

    all_layer_outputs = []
    all_layer_attention_probs = []
    for layer_idx in range(config.num_hidden_layers):
        with tf.variable_scope("layer_%d" % layer_idx):
            layer_input = prev_output  # 当前层的输入等于上一层的输出
            layer_output = bert_transformer_layer(
                layer_input=layer_input,
                batch_size=batch_size,
                seq_length=seq_length,
                attention_mask=attention_mask,
                hidden_size=config.hidden_size,  # 隐藏层大小
                num_attention_heads=config.num_attention_heads,
                intermediate_size=config.intermediate_size,
                intermediate_act_fn=get_activation(config.hidden_act),
                hidden_dropout_prob=config.hidden_dropout_prob,
                attention_probs_dropout_prob=config.attention_probs_dropout_prob,
                initializer_range=config.initializer_range,
                use_relative_position=True,  # nezha 模型使用相对位置信息
                do_return_attentions_probs=config.output_attentions
            )
            prev_output = layer_output[0]
            if config.output_hidden_states:
                all_layer_outputs.append(layer_output[0])
            if config.output_attentions:
                all_layer_attention_probs.append(layer_output[1])

    final_output = reshape_from_matrix(prev_output, input_shape)

    outputs = (final_output,)

    if config.output_hidden_states:
        final_all_layer_outputs = []
        for layer_output in all_layer_outputs:
            final_output = reshape_from_matrix(layer_output, input_shape)
            final_all_layer_outputs.append(final_output)
        outputs = outputs + (final_all_layer_outputs,)

    if config.output_attentions:
        outputs = outputs + (all_layer_attention_probs,)
    return outputs  # (last layer output, all layer outputs, all layer att probs)


class NeZhaModel(BaseModel):
    def __init__(
            self,
            config,
            input_ids,
            input_mask=None,
            token_type_ids=None,
            is_training=None,
            scope=None,
            reuse=False
    ):
        super().__init__(config, is_training)

        input_shape = get_shape_list(input_ids, expected_rank=2)
        batch_size = input_shape[0]
        seq_length = input_shape[1]

        if input_mask is None:
            input_mask = tf.ones(shape=[batch_size, seq_length], dtype=tf.int32)

        if token_type_ids is None:
            token_type_ids = tf.zeros(shape=[batch_size, seq_length], dtype=tf.int32)

        with tf.variable_scope(scope, default_name="bert",
                               reuse=tf.AUTO_REUSE if reuse else None):
            with tf.variable_scope("embeddings"):
                self.embedding_output, self.embedding_table = NeZhaEmbedding(
                    config=self.config,
                    input_ids=input_ids,
                    token_type_ids=token_type_ids
                )

            with tf.variable_scope("encoder"):
                attention_mask = create_attention_mask_from_input_mask(
                    input_ids, input_mask)
                encoder_outputs = NeZhaEncoder(
                    config=self.config,
                    input_tensor=self.embedding_output,
                    attention_mask=attention_mask
                )

            with tf.variable_scope("pooler"):
                pooled_output = pooler_layer(
                    sequence_output=encoder_outputs[0],
                    hidden_size=self.config.hidden_size,
                    initializer_range=self.config.initializer_range
                )
        # (pooled output, sequence output, all layer outputs, all layer att probs)
        self.outputs = (pooled_output,) + encoder_outputs
