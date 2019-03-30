import math
import numpy as np
import cntk as C
import cntkx as Cx
from cntkx.layers.blocks import WeightMaskedLSTM, _INFERRED
from cntkx.layers.sequence import VariationalDropout
from cntk.default_options import default_override_or
from cntk.layers.blocks import identity, _initializer_for, _inject_name
from cntk.layers import SequentialConvolution, Recurrence, Embedding, Dropout
from cntk.layers import MaxPooling, Convolution2D, LayerNormalization
from cntk.internal import _as_tuple
from cntk.variables import Record


# TODO: To be removed once the main cntk line accepts the pull request to fix initialisation bug
def Dense(shape, activation=default_override_or(identity), init=default_override_or(C.glorot_uniform()),
          input_rank=None, map_rank=None, bias=default_override_or(True), init_bias=default_override_or(0), name=''):
    '''
    Dense(shape, activation=identity, init=glorot_uniform(), input_rank=None, map_rank=None, bias=True, init_bias=0, name='')

    Layer factory function to create an instance of a fully-connected linear layer of the form
    `activation(input @ W + b)` with weights `W` and bias `b`, and `activation` and `b` being optional.
    `shape` may describe a tensor as well.

    A ``Dense`` layer instance owns its parameter tensors `W` and `b`, and exposes them as attributes ``.W`` and ``.b``.

    Example:
     >>> f = Dense(5, activation=C.relu)
     >>> x = C.input_variable(3)
     >>> h = f(x)
     >>> h.shape
         (5,)
     >>> f.W.shape
         (3, 5)
     >>> f.b.value
         array([ 0.,  0.,  0.,  0.,  0.], dtype=float32)

     >>> # activation through default options
     >>> with C.default_options(activation=C.relu):
     ...     f = Dense(500)

    The ``Dense`` layer can be applied to inputs that are tensors, not just vectors.
    This is useful, e.g., at the top of a image-processing cascade, where after many
    convolutions with padding and strides it is difficult to know the precise dimensions.
    For this case, CNTK has an extended definition of matrix product, in which
    the input tensor will be treated as if it had been automatically flattened.
    The weight matrix will be a tensor that reflects the "flattened" dimensions in its axes.

    Example:
     >>> f = Dense(5, activation=C.softmax) # a 5-class classifier
     >>> x = C.input_variable((64,16,16)) # e.g. an image reduced by a convolution stack
     >>> y = f(x)
     >>> y.shape
     (5,)
     >>> f.W.shape  # "row" dimension of "matrix" consists of 3 axes that match the input
     (64, 16, 16, 5)

    This behavior can be modified by telling CNTK either the number of axes that should not be projected (``map_rank``)
    or the rank of the input (``input_rank``). If neither is specified, all input dimensions are
    projected, as in the example above.

    Example:
     >>> f = Dense(5, activation=C.softmax, input_rank=2) # a 5-class classifier
     >>> x = C.input_variable((10, 3, 3)) # e.g. 10 parallel 3x3 objects. Input has input_rank=2 axes
     >>> y = f(x)
     >>> y.shape  # the 10 parallel objects are classified separately, the "10" dimension is retained
     (10, 5)
     >>> f.W.shape  # "row" dimension of "matrix" consists of (3,3) matching the input axes to project
     (3, 3, 5)

     >>> f = Dense(5, activation=C.softmax, map_rank=2)
     >>> x = C.input_variable((4, 6, 3, 3, 3)) # e.g. 24 parallel 3x3x3 objects arranged in a 4x6 grid. The grid is to be retained
     >>> y = f(x)
     >>> y.shape  # the 4x6 elements are classified separately, the grid structure is retained
     (4, 6, 5)
     >>> f.W.shape  # "row" dimension of "matrix" consists of (3,3) matching the input axes to project
     (3, 3, 3, 5)
     >>> z = y([np.zeros(x.shape)])
     >>> assert z.shape == (1, 4, 6, 5)

    Args:
     shape (`int` or `tuple` of `ints`): vector or tensor dimension of the output of this layer
     activation (:class:`~cntk.ops.functions.Function`, defaults to identity): optional function to apply at the end, e.g. `relu`
     init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to :func:`~cntk.initializer.glorot_uniform` ): initial value of weights `W`
     input_rank (int, defaults to `None`): number of inferred axes to add to W (`map_rank` must not be given)
     map_rank (int, defaults to `None`): expand W to leave exactly `map_rank` axes (`input_rank` must not be given)
     bias (bool, optional, defaults to `True`): the layer will have no bias if `False` is passed here
     init_bias (scalar or NumPy array or :mod:`cntk.initializer`, defaults to 0): initial value of weights `b`
     name (str, defaults to ''): the name of the function instance in the network

    Returns:
        cntk.ops.functions.Function:
        A function that accepts one argument and applies the operation to it
    '''

    activation = C.get_default_override(Dense, activation=activation)
    init       = C.get_default_override(Dense, init=init)
    bias       = C.get_default_override(Dense, bias=bias)
    init_bias  = C.get_default_override(Dense, init_bias=init_bias)

    output_shape = _as_tuple(shape)

    if input_rank is not None and map_rank is not None:
        raise ValueError("Dense: input_rank and map_rank cannot be specified at the same time.")

    # determine meaning of axes
    # W gets dimension (input_shape + shape)
    # where input_shape is determined as:
    #  - by default, equal to the dimensions of the input passed to Dense()
    #  - if input_rank is given, then the last 'input_rank' dimensions of the input (all others are not reduced over)
    #  - if map_rank is given, then the all but the first 'map_rank' dimensions of the input (those are not reduced over)
    # where input_rank and map_rank are mutually exclusive.

    output_rank = len(output_shape)   # support outputs with tensor layouts

    # If input_rank not given then pass a single _INFERRED; map_rank if given will determine the input_rank.
    # The dimension inference may still create multiple axes.
    input_shape = _INFERRED * (input_rank if input_rank is not None else 1)

    if input_rank is not None:
        infer_input_rank_to_map = -1 # means map_rank is not specified; input_rank rules
    elif map_rank is None:
        infer_input_rank_to_map = 0  # neither given: default to 'infer W to use all input dims'
    else:
        infer_input_rank_to_map = map_rank  # infer W to use all input dims except the first static 'map_rank' ones

    # parameters bound to this Function
    if isinstance(init, np.ndarray):
        init_weights = init
    else:
        init_weights = _initializer_for(init, Record(output_rank=output_rank))

    W = C.Parameter(input_shape + output_shape, init=init_weights, name='W')
    b = C.Parameter(              output_shape, init=init_bias,    name='b') if bias else None

    # expression of this function
    @C.BlockFunction('Dense', name)
    def dense(x):
        r = C.times(x, W, output_rank=output_rank, infer_input_rank_to_map=infer_input_rank_to_map)
        if b:
            r = r + b
        if activation is not None:
            r = activation(r)
        return r
    return dense


def QRNN(window: int = 1, hidden_dim=None, activation=C.tanh, return_full_state=False, name=''):
    """
    Quasi-Recurrent Neural Networks layer

    This is the CNTK implementation of [Salesforce Research](https://einstein.ai/)'s
    [Quasi-Recurrent Neural Networks](https://arxiv.org/abs/1611.01576) paper.

    More details on tuning and application can be found in this paper:
    [An Analysis of Neural Language Modeling at Multiple Scales](https://arxiv.org/abs/1803.08240)

    QRNN is used in hangwriting recognition in Gboard too. More details in following link:
    https://ai.googleblog.com/2019/03/rnn-based-handwriting-recognition-in.html

    From the authors:
        The QRNN provides similar accuracy to the LSTM but can be between
        2 and 17 times faster than the highly optimized NVIDIA cuDNN LSTM
        implementation depending on the use case.
        If you use this code or our results in your research, please cite:
        @article{bradbury2016quasi,
          title={{Quasi-Recurrent Neural Networks}},
          author={Bradbury, James and Merity, Stephen and Xiong, Caiming and Socher, Richard},
          journal={International Conference on Learning Representations (ICLR 2017)},
          year={2017}
        }

    Examples:
        input_tensor = C.sequence.input_variable(input_dim)

        hidden = QRNN(window=2, hidden_dim=hidden_dim)(input_tensor)
        prediction = Dense(1)(C.sequence.last(hidden))

    Arguments:
        window (`int`):  Defines the size of the convolutional window (how many previous
          tokens to look when computing the QRNN values). Defaults 1.
        hidden_dim (int): size of hidden dim of h, c and o
        activation: cell activation function
        return_full_state: if to return cell and hidden states. Default false.
        name: name of function instance in network

    Returns:
        :class:`~cntk.ops.functions.Function`: OR
        tuple of :class:`~cntk.ops.functions.Function`:

    """

    sequential_conv = SequentialConvolution(filter_shape=(window,),
                                            num_filters=3 * hidden_dim,
                                            pad=False,
                                            reduction_rank=1,
                                            name='conv')

    @C.Function
    def f_pool(c, zf):
        z = C.slice(zf, 0, 0, hidden_dim)
        f = C.slice(zf, 0, hidden_dim, 2 * hidden_dim)
        return f * c + (1 - f) * z

    @C.BlockFunction('QRNN', name)
    def model(input_tensor):

        input_sequence = input_tensor
        if window > 1:
            # to ensure causal relation is still preserved
            input_sequence = Cx.sequence.pad(input_sequence, (window - 1, 0), constant_value=0)

        gate_values = sequential_conv(input_sequence)

        x = C.slice(gate_values, -1, 0, hidden_dim)
        forget = C.slice(gate_values, -1, hidden_dim, 2 * hidden_dim)
        output = C.slice(gate_values, -1, 2 * hidden_dim, 3 * hidden_dim)

        z = activation(x)
        f = C.sigmoid(forget)
        o = C.sigmoid(output)

        # Pooling
        c = Recurrence(f_pool)(C.splice(z, f))  # f pool
        h = o * c  # o pool

        if return_full_state:
            return h, c
        else:
            return h

    return model


def SinusoidalPositionalEmbedding(min_timescale=1.0, max_timescale=1.0e4, name: str = ''):
    """ Gets a bunch of sinusoids of different frequencies and add it to the input sequence

    Each channel of the input Tensor is incremented by a sinusoid of a different
    frequency and phase. This allows attention to learn to use absolute and relative positions.

    Timing signals should be added to some precursors of both the query and the
    memory inputs to attention. The use of relative position is possible because
    sin(x+y) and cos(x+y) can be expressed in terms of y, sin(x) and cos(x).

    In particular, we use a geometric sequence of timescales starting with
    min_timescale and ending with max_timescale. The number of different
    timescales is equal to channels / 2. For each timescale, we
    generate the two sinusoidal signals sin(timestep/timescale) and
    cos(timestep/timescale).  All of these sinusoids are concatenated in
    the channels dimension.

    This matches the implementation in tensor2tensor, but differs slightly
    from the description in Section 3.5 of "Attention Is All You Need" in
    that if input_dim is odd, the last dim will be a zero value.

    This implementation is equivalent to get_timing_signal_1d() in tensorflow's tensor2tensor:
        https://github.com/tensorflow/tensor2tensor/blob/23bd23b9830059fbc349381b70d9429b5c40a139/
          tensor2tensor/layers/common_attention.py

    There are no learnable parameters in this embedding.

    Example:
        import cntk as C
        import cntkx as Cx

        a = C.sequence.input_variable(10)
        b = Cx.layers.SinusoidalPositionalEmbedding()(a)

        assert b.shape == (10, )

    Arguments:
        min_timescale (float): geometric sequence of timescales starting with min_timescale
        max_timescale (float): geometric sequence of timescales ending with max_timescale
        name (str): a name for this layer.

    Returns:
        :class:`~cntk.ops.functions.Function`: same shape as input sequence tensor

    """

    @C.Function
    def position(p, x):
        return p + x * 0 + 1

    def embedding(x):
        assert x.shape[0] > 0, f"input tensor must have a defined shape, input shape is {x.shape}"
        dim = x.shape[0]
        num_timescales = dim // 2
        log_timescale_increment = (math.log(float(max_timescale) / float(min_timescale)) / (num_timescales - 1))
        inv_timescales = C.constant(min_timescale * np.exp(np.arange(num_timescales) * -log_timescale_increment),
                                    dtype=np.float32)

        pos = Recurrence(position)(C.slice(x, 0, 0, num_timescales))
        scaled_time = pos * inv_timescales
        s = C.sin(scaled_time)
        c = C.cos(scaled_time)
        signal = C.splice(s, c)

        # last dim gets a 0 value if input_dim is odd
        if dim % 2 != 0:
            signal = C.pad(signal, [[0, 1]])

        return C.layers.Label(name=name)(signal)

    return embedding


def Conv2DMaxPool(n, conv_filter_shape,  # shape of receptive field, e.g. (3,3). Must be a 2-element tuple.
                  pool_filter_shape,  # shape of receptive field, e.g. (3,3)
                  conv_num_filters=None,  # e.g. 64 or None (which means 1 channel and don't add a dimension)
                  activation=default_override_or(identity),
                  init=default_override_or(C.glorot_uniform()),
                  conv_pad=default_override_or(False),
                  conv_strides=1,
                  bias=default_override_or(True),
                  init_bias=default_override_or(0),
                  reduction_rank=1,  # (0 means input has no depth dimension, e.g. audio signal or B&W image)
                  dilation=1,
                  groups=1,
                  pool_strides=1,
                  pool_pad=default_override_or(False),
                  name_prefix=''):
    """ Stack of Convolution 2D followed by one max pooling layer. Convenience wrapper. """

    conv_stack = Convolution2DStack(n, conv_filter_shape, conv_num_filters, activation, init, conv_pad, conv_strides,
                                    bias, init_bias, reduction_rank, dilation, groups, name_prefix)

    maxpool = MaxPooling(pool_filter_shape, pool_strides, pool_pad, name_prefix + '_pool')

    def layer(x):
        x = conv_stack(x)
        x = maxpool(x)
        return x

    return layer


def Convolution2DStack(num_conv_layers,  # num of convolutional layers in the stack
                       filter_shape,  # shape of receptive field, e.g. (3,3). Must be a 2-element tuple.
                       num_filters=None,  # e.g. 64 or None (which means 1 channel and don't add a dimension)
                       activation=default_override_or(identity),
                       init=default_override_or(C.glorot_uniform()),
                       pad=default_override_or(False),
                       strides=1,
                       bias=default_override_or(True),
                       init_bias=default_override_or(0),
                       reduction_rank=1,  # (0 means input has no depth dimension, e.g. audio signal or B&W image)
                       dilation=1,
                       groups=1,
                       name_prefix=''):
    """ A stack of of convolutional layers. Convenience wrapper. """

    convs = [Convolution2D(filter_shape, num_filters, activation, init, pad, strides, bias,
                           init_bias, reduction_rank, dilation, groups,
                           name_prefix + f'_conv_{i}') for i in range(num_conv_layers)]

    def inner(x):

        for conv in convs:
            x = conv(x)

        return x

    return inner


def SpatialPyramidPooling(bins: tuple, name=''):
    """ Spatial pyramid pooling layer for 2D inputs.

    See Spatial Pyramid Pooling in Deep Convolutional Networks for Visual Recognition,
    K. He, X. Zhang, S. Ren, J. Sun (https://arxiv.org/abs/1406.4729)

    SSP is used for multi-sized training where during training we implement the varying-input-size SPP-net
    by two fixed-size networks that share parameters. SSP layer will be different for the 2 network that
    shares parameters since the SSP would have different windows and stride.

    The final output shape would be input_num_filters * reduce_sum(square(bins))
    e.g. bins = (1, 3, 5) and input_num_filters = 32 then output_shape = (32 * (1 * 1 + 3 * 3 + 5 * 5), ) regardless
    of input feature map's spatial dimension.

    Arguments:
        bins (tuple): tuple of ints stating the depth of the pyramid and number of bins at each level.
        name (str, optional): name of layer

    Returns:
        :class:`~cntk.ops.functions.Function`:

    """

    def spp(x):
        spatial = x.shape[1:]
        filter_shapes = [tuple(math.ceil(s / bin) for s in spatial) for bin in bins]
        strides = [tuple(math.floor(s / bin) for s in spatial) for bin in bins]

        pools = [MaxPooling(filter_shape, stride, pad=False) for filter_shape, stride in zip(filter_shapes, strides)]
        features = [C.flatten(pool(x)) for pool in pools]
        return C.squeeze(C.splice(*features), name=name)

    return spp


def SequentialStride(input_ndim: int, dim_axis0: int, stride: int = 1, pad: bool = True, name: str = ''):
    """ Strides across the sequential axis

    Example:
        a = C.sequence.input_variable((3, 10))
        b = SequentialStride(input_ndim=2, dim_axis0=3, stride=2, pad=False)(a

        assert b.shape == a.shape
        # b has all odd sequence element due to stride=2

    Arguments:
        input_ndim (int): number of dimensions in input tensor
        dim_axis0 (int): dimension of first axis of input tensor, i.e. input_tensor.shape[0]
        stride (int): stride across sequential axis
        pad (bool): whether to pad sequential axis
        name (str): name of function instance in network

    Returns:
        :class:`~cntk.ops.functions.Function`:

    """

    # create dummy pad/stride/filter for static axes
    num_static_axes = (input_ndim - 1)
    conv_pad = (pad, ) + (False,) * num_static_axes
    conv_strides = (stride, ) + (1,) * num_static_axes
    conv_filter = (1,) + (1,) * num_static_axes
    conv_num_filters = dim_axis0

    assert len(conv_pad) == len(conv_strides) == len(conv_filter) == 1 + num_static_axes

    identity_kernel = np.eye(conv_num_filters, conv_num_filters)
    identity_kernel = identity_kernel.reshape((conv_num_filters, conv_num_filters) + (1,) * len(conv_filter))

    for i, d in enumerate(conv_filter):
        identity_kernel = np.repeat(identity_kernel, d, axis=2 + i)

    # print('===========================================')
    # print('Sequential Stride')
    # print('-------------------------------------------')
    # print(f'conv_num_filters:   {conv_num_filters}')
    # print(f'conv_filter:        {conv_filter}')
    # print(f'conv_strides:       {conv_strides}')
    # print(f'conv_pad:           {conv_pad}')
    # print(f'identity_kernel:    {identity_kernel.shape}')
    # print('===========================================')

    # set identity kernel into convolution layer
    dummy_input_shape = (conv_num_filters, ) + (1,) * num_static_axes
    dummy = C.sequence.input_variable(shape=dummy_input_shape, name='dummy')

    strider = SequentialConvolution(filter_shape=conv_filter, num_filters=conv_num_filters,
                                    bias=False, pad=conv_pad, strides=conv_strides, name='strider')
    temp = strider(dummy)  # to initialise inferred dimension in kernel
    strider.W.value = identity_kernel
    strider = temp.clone(C.CloneMethod.freeze, {dummy: C.placeholder()})  # to work around bug

    @C.BlockFunction('SequentialStride', name)
    def inner(x):
        return strider(x)

    return inner


def SequentialMaxPooling(filter_shape,  # shape of receptive field, e.g. (3,3). filter_shape[0] is for sequence axis.
                         strides=1,     # strides[0] is for sequence axis.
                         pad=default_override_or(True),   # pad[0] is for sequence axis.
                         name=''):
    """ Layer factory function to create a max-pooling layer that works with sequences

    Sequential max pooling has a slight bug in that even when Pad=False, sequence axis will still be
    padded and asymmetrically padded so on the right. i.e. there may be an extrac sequence element. But it should
    not be an issue since error in border pixels typically wouldn't affect results.

    Example:
        # rgb image of height 25 and variable width
        a = C.sequence.input_variable((3, 25))

        # max pool (2,2) in height and width with stride (2,2) in height and width, no padding
        b = SequentialMaxPooling(filter_shape=(2, 2), strides=(2, 2), pad=False)(a)
        assert b.shape == (3, 12)

        # max pool (2,2) in height and width with stride (2,2) in height and width, with padding
        b = SequentialMaxPooling(filter_shape=(2, 2), strides=(2, 2), pad=True)(a)
        assert b.shape == (3, 13)


    Arguments:
        filter_shape (`int` or `tuple` of `ints`): shape (spatial extent) of the receptive field, *not* including the input feature-map depth. E.g. (3,3) for a 2D convolution.
        strides (`int` or `tuple` of `ints`, defaults to 1): stride (increment when sliding over the input). Use a `tuple` to specify a per-axis value.
        pad (`bool` or `tuple` of `bools`, defaults to `False`): if `False`, then the pooling operation will be shifted over the "valid"
          area of input, that is, no value outside the area is used. If ``pad=True`` on the other hand,
          pooling will be applied to all input positions, and positions outside the valid region will be considered containing zero.
          Use a `tuple` to specify a per-axis value.
        name (str, defaults to ''): the name of the function instance in the network


    Returns:
        cntk.ops.functions.Function:
        A function that accepts one argument and applies the max-pooling operation to it

    """
    assert isinstance(filter_shape, tuple), "filter must be a tuple"

    if not isinstance(pad, tuple):
        pad = tuple(pad for __ in filter_shape)

    if not isinstance(strides, tuple):
        strides = tuple(strides for __ in filter_shape)

    # for pooling in static axes
    pool_filter_shape = filter_shape[1:]
    pool_pad = pad[1:]
    pool_strides = strides[1:]

    window_dim = 1 + 1 + len(filter_shape[1:])  # concat axis + channel axis + any other static axes
    seq_stride = SequentialStride(input_ndim=window_dim,             # concat axis
                                  dim_axis0=filter_shape[0],         # window/kernel size in seq axis
                                  stride=strides[0],
                                  pad=pad[0],
                                  name='seq_stride')

    # static_pool over (channel_static_axis, height_static_axis)
    if pool_filter_shape and pool_strides and pool_pad:
        static_pooler = MaxPooling(filter_shape=filter_shape[1:], strides=pool_strides, pad=pool_pad, name='static_pooler')
    else:
        static_pooler = identity  # when there is no static axes to pool

    @C.BlockFunction('SequentialMaxPooling', name)
    def inner(x):
        if pad[0]:  # sequential axis
            # when kernel is even, padding will be asymmetric in left and right
            right_pad = int((filter_shape[0] - 1) / 2) if filter_shape[0] % 2 else int(filter_shape[0] / 2)
            left_pad = right_pad if filter_shape[0] % 2 else right_pad - 1

            past = [C.sequence.past_value(x, time_step=i + 1) for i in range(left_pad)]
            future = [C.sequence.future_value(x, time_step=i + 1) for i in range(right_pad)]

            past_now_future = past + [x] + future

        else:

            future = [C.sequence.future_value(x, time_step=i + 1) for i in range(filter_shape[1] - 1)]
            past_now_future = [x] + future

        windows = C.splice(*past_now_future, axis=C.Axis.new_leading_axis())
        # windows: [#, *] [concat, channel, static_axes...]

        selected_windows = seq_stride(windows)
        # selected_windows: [#, **] [concat, channel, static_axes...]

        # Pooling between sequential elements done by reduce_max on windows
        # BUGBUG: do not set keepdims=False in reduce_max, will raise error
        sequential_max_pooled = C.squeeze(C.reduce_max(selected_windows, axis=0), axes=0)
        # sequential_max_pooled: [#, **] [channel, static_axes...]

        pooled = static_pooler(sequential_max_pooled)
        # sequential_max_pooled: [#, **] [channel, pooled_static_axes...]

        return pooled

    return inner


def GatedLinearUnit(window: int = 2, hidden_dim: int = None, activation=C.sigmoid):
    """
    Gated Linear Unit or gated convolutional neural network is a finite context approach
    through stacked convolutions, which can be  more  efficient  since  they  allow
    parallelization over sequential tokens.

    Context is captured through the stacking multiple gated linear units with window size more than one unlike
    in QRNN where there is still an explicit recurrence/pooling relationship temporally.

    Example:
        a = C.sequence.input_variable(56)
        b = Cx.layers.GatedLinearUnit(2, 100)(a)

        assert b.shape == (100, )

    Arguments:
        window (`int`):  Defines the size of the convolutional window (how many previous
          tokens to look when computing the gated linear unit values). Defaults 2.
        hidden_dim (int): size of hidden output dim. Must be divisible by 2.
        activation: gate function

    Returns:
        :class:`~cntk.ops.functions.Function`:

    """
    assert hidden_dim % 2 == 0, "hidden dimension must be divisible by 2"

    def inner(input_tensor):
        filter_shape = (window,) + input_tensor.shape

        input_sequence = input_tensor
        if window > 1:
            # to ensure causal relation is still preserved
            input_sequence = Cx.sequence.pad(input_sequence, (window - 1, 0), constant_value=0)

        conv_values = SequentialConvolution(filter_shape=filter_shape, num_filters=2 * hidden_dim, pad=False,
                                            reduction_rank=0)(input_sequence) >> C.squeeze

        outputs = C.slice(conv_values, 0, 0, hidden_dim) + activation(C.slice(conv_values, 0, hidden_dim, 2 * hidden_dim))
        return outputs

    return inner


def PositionalEmbedding(max_seq_length: int, hidden_dim: int, init=default_override_or(C.glorot_uniform()),
                        weights=None, name: str = ''):
    """ Learnable positional embedding

    Example:
        a = C.sequence.input_variable(5)
        positional_embedding =

    Arguments:
        max_seq_length (int): max sequence length embeddable
        hidden_dim (int): dimension of the embedding vector
        init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to :func:`~cntk.initializer.glorot_uniform` ): (learnable embedding only) initial value of weights `E`
        weights (NumPy array, mutually exclusive with ``init``, defuats to `None`): (user-supplied embedding only) the lookup table.
          The matrix rows are the embedding vectors, ``weights[i,:]`` being the embedding that corresponds to input category `i`.
        name (str): name of the layer

    Returns:
        :class:`~cntk.ops.functions.Function`:
        Positional embedding vector of shape (`hidden_dim`, )
    """

    position_embeddings = Embedding(shape=hidden_dim, init=init, weights=weights, name='PE')

    @C.BlockFunction('PositionalEmbedding', name)
    def inner(x):
        position_index = Cx.sequence.position(x)
        pos = C.one_hot(position_index, max_seq_length, sparse_output=True) >> C.squeeze
        embedded = position_embeddings(pos)
        return embedded

    return inner


def BertEmbeddings(max_seq_length, hidden_dim: int = None, dropout_rate: float = None,
                   word_embed_init=default_override_or(C.glorot_uniform()), word_embed_weights=None,
                   position_embed_init=default_override_or(C.glorot_uniform()), position_embed_weights=None,
                   token_type_embed_init=default_override_or(C.glorot_uniform()), token_type_embed_weights=None,
                   layer_norm_init_scale=1, layer_norm_init_bias=0, name=''):
    """ Construct the embeddings from word, position and token_type embeddings that is used in BERT.
    Paper can be found at https://arxiv.org/abs/1810.04805 (BERT: Pre-training of Deep Bidirectional
    Transformers for Language Understanding)

    Arguments:
        max_seq_length (int): max sequence length possible for positional embedding
        hidden_dim (int): dimension of the embedding vector
        dropout_rate (float): probability of dropout
        layer_norm_init_scale (float): initial value for the ``scale`` parameter
        layer_norm_init_bias (float): initial value for the ``bias`` parameter

    Returns:
        :class:`~cntk.ops.functions.Function`:
        Embedding vector of shape (`hidden_dim`, )

    """
    word_embeddings = Embedding(shape=hidden_dim, init=word_embed_init, weights=word_embed_weights, name='word_embeddings')
    position_embeddings = PositionalEmbedding(max_seq_length, hidden_dim=hidden_dim, init=position_embed_init, weights=position_embed_weights, name='position_embeddings')
    token_type_embeddings = Embedding(shape=hidden_dim, init=token_type_embed_init, weights=token_type_embed_weights, name='token_type_embeddings')  # aka 'segment embedding'

    layer_norm = LayerNormalization(initial_scale=layer_norm_init_scale, initial_bias=layer_norm_init_bias,
                                    name='LayerNorm')

    dropout = Dropout(dropout_rate, name='dropout')

    @C.BlockFunction('BertEmbeddings', name)
    def inner(text_tensor, token_type_tensor):
        embedded_word_tensors = word_embeddings(text_tensor)
        embedded_token_type_tensors = token_type_embeddings(token_type_tensor)
        embedded_position_tensors = position_embeddings(text_tensor)

        embedded_tensor = embedded_word_tensors + embedded_position_tensors + embedded_token_type_tensors
        embedded_tensor = layer_norm(embedded_tensor)
        embedded_tensor = dropout(embedded_tensor)
        return embedded_tensor

    return inner


def PreTrainedBertEmbeddings(tf_bert_model_filepath: str, dropout_rate: float = None, name=''):
    """ Use pre-trained tensorflow bert model to initialise the model

    Currently it is tested to work with:
        - `BERT-Base, Uncased`, uncased_L-12_H-768_A-12

    Models can be downloaded at https://github.com/google-research/bert

    Arguments:
        tf_bert_model_filepath (str): file path to the tensorflow model
        dropout_rate (float): probability of dropping out an element
        learnable (bool): True if training of embeddings is desired. Defaults to False.

    Returns:
        :class:`~cntk.ops.functions.Function`:
        TF to CNTK Pre-trained Bert Embeddings vector
    """

    try:
        import tensorflow as tf
    except ImportError:
        raise ImportError("Loading a TensorFlow models in CNTK, requires TensorFlow to be installed. Please see "
                          "https://www.tensorflow.org/install/ for installation instructions.")

    bert_embedding = 'bert/embeddings/'
    layer_names = [f'{bert_embedding}LayerNorm/beta',
                   f'{bert_embedding}LayerNorm/gamma',
                   f'{bert_embedding}position_embeddings',
                   f'{bert_embedding}token_type_embeddings',
                   f'{bert_embedding}word_embeddings']

    variables_meta = [meta for meta in tf.train.list_variables(tf_bert_model_filepath) if meta[0] in layer_names]
    pretrained_weights = [tf.train.load_variable(tf_bert_model_filepath, meta[0]) for meta in variables_meta]
    pretrained_variables = [(n, shape, weight) for weight, (n, shape) in zip(pretrained_weights, variables_meta)]

    layernorm_beta_embed_variables = pretrained_variables[0]  # bias
    layernorm_gamma_embed_variables = pretrained_variables[1]  # scale
    position_embed_variables = pretrained_variables[2]
    token_type_embed_variables = pretrained_variables[3]
    word_embed_variables = pretrained_variables[4]

    pretrained_bert_embedding = BertEmbeddings(max_seq_length=position_embed_variables[1][0],
                                               hidden_dim=1,  # this argument must be declared and will be ignored
                                               dropout_rate=dropout_rate,
                                               word_embed_init=word_embed_variables[-1],
                                               position_embed_init=position_embed_variables[-1],
                                               token_type_embed_init=token_type_embed_variables[-1],
                                               layer_norm_init_scale=layernorm_gamma_embed_variables[-1],
                                               layer_norm_init_bias=layernorm_beta_embed_variables[-1],
                                               name=name)

    return pretrained_bert_embedding


def BertPooler(shape, init=default_override_or(C.glorot_uniform()), init_bias=default_override_or(0), name=''):
    """ Bert Pooler layer

    We "pool" the model by simply taking the hidden state corresponding to the first token.

    Arguments:
        shape (`int` or `tuple` of `ints`): vector or tensor dimension of the output of this layer
        init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to :func:`~cntk.initializer.glorot_uniform` ): initial value of weights `W`
        init_bias (scalar or NumPy array or :mod:`cntk.initializer`, defaults to 0): initial value of weights `b`
        name (str, defaults to ''): the name of the function instance in the network

    Returns:
        :class:`~cntk.ops.functions.Function`:

    """
    dense = Dense(shape=shape, activation=C.tanh, init=init, init_bias=init_bias)

    @C.BlockFunction('BertPooler', name)
    def inner(x):

        return dense(C.sequence.first(x))

    return inner


def PretrainedBertPooler(tf_bert_model_filepath: str):
    """ Pre-trained bert pooler converted from the tensorflow model


    """
    try:
        import tensorflow as tf
    except ImportError:
        raise ImportError("Loading a TensorFlow models in CNTK, requires TensorFlow to be installed. Please see "
                          "https://www.tensorflow.org/install/ for installation instructions.")

    pretrained_bert_pooler = BertPooler((None, ),  # shape is not necessary when init from np array
                                        init=tf.train.load_variable(tf_bert_model_filepath, "bert/pooler/dense/kernel"),
                                        init_bias=tf.train.load_variable(tf_bert_model_filepath, "bert/pooler/dense/bias"),
                                        name='pooler')

    return pretrained_bert_pooler


def WeightDroppedLSTM(shape, dropconnect_rate: float = None, variational_dropout_rate_input: float = None,
                      variational_dropout_rate_output: float = None,
                      activation=default_override_or(C.tanh), use_peepholes=default_override_or(False),
                      init=default_override_or(C.glorot_uniform()), init_bias=default_override_or(0),
                      enable_self_stabilization=default_override_or(False), go_backwards=default_override_or(False),
                      initial_state=default_override_or(0), return_full_state=False, name=''):
    """ LSTM recurence layer with DropConnect and variational dropout applied

    Weight dropped is implemented as DropConnect of hidden-to-hidden weight matrics, not the dropout of
    hidden states (aka variational dropout).

    For more details on Weight-Dropped LSTM, please read "regularizing and optimizing LSTM language models"
    by S. Merity, at el (https://arxiv.org/abs/1708.02182)

    Weight masked LSTM step function is available in cntkx.layers.blocks as WeightMaskedLSTM.

    Note that in typical usage, the output of the last `WeightDroppedLSTM` layer in the rnn layer stack
    should not be variationally dropped out (i.e. variational_dropout_rate_output should be set to zero).
    This is advice is consistent with salesforce's implementation of awd-lstm
    (https://github.com/salesforce/awd-lstm-lm/blob/master/model.py)

    Examples:
        a = C.sequence.input_variable(10)
        b = Cx.layers.WeightDroppedLSTM(20, 0.1, 0.1, 0.1)(a)

        assert b.shape == (20, )

    Arguments:
        shape (`int` or `tuple` of `ints`): vector or tensor dimension of the output of this layer
        dropconnect_rate (float): probability of dropping out an element in dropconnect
        variational_dropout_rate_input (float): probability of dropping out an input element
        variational_dropout_rate_output (float): probability of dropping out an output element
        activation (:class:`~cntk.ops.functions.Function`, defaults to :func:`~cntk.ops.tanh`): function to apply at the end, e.g. `relu`
        use_peepholes (bool, defaults to `False`):
        init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to `glorot_uniform`): initial value of weights `W`
        init_bias (scalar or NumPy array or :mod:`cntk.initializer`, defaults to 0): initial value of weights `b`
        enable_self_stabilization (bool, defaults to `False`): if `True` then add a :func:`~cntk.layers.blocks.Stabilizer`
         to all state-related projections (but not the data input)
        go_backwards (bool, defaults to ``False``): if ``True`` then run the recurrence from the end of the sequence to the start.
        initial_state (scalar or tensor without batch dimension; or a tuple thereof):
          the initial value for the state. This can be a constant or a learnable parameter.
          In the latter case, if the step function has more than 1 state variable,
          this parameter must be a tuple providing one initial state for every state variable.
        return_full_state (bool, defaults to ``False``): if ``True`` and the step function has more than one
          state variable, then the layer returns a all state variables (a tuple of sequences);
          whereas if not given or ``False``, only the first state variable is returned to the caller.
        name (str, defaults to ''): the name of the Function instance in the network

    """
    dropout = C.layers.Dropout(dropconnect_rate)
    variational_dropout_input = VariationalDropout(variational_dropout_rate_input) if variational_dropout_rate_input > 0 else None
    variational_dropout_output = VariationalDropout(variational_dropout_rate_output) if variational_dropout_rate_output > 0 else None

    lstm = WeightMaskedLSTM(shape=shape, activation=activation, use_peepholes=use_peepholes, init=init, init_bias=init_bias,
                            enable_self_stabilization=enable_self_stabilization, name='WDLSTMCell')

    @C.Function
    def inner(x):

        # mask for hidden-to-hidden weight that is the same for all temporal steps
        dummy = C.slice(C.sequence.first(x), 0, 0, 1)
        drop_connect = dropout(C.zeros_like(lstm.parameters[-1]) * dummy + C.constant(1))
        drop_connect = C.sequence.broadcast_as(drop_connect, x)

        @C.Function
        def weight_dropped_lstm(h, c, x):

            a, b, __ = lstm(h, c, drop_connect, x).outputs
            return a, b

        x = variational_dropout_input(x) if variational_dropout_input else x
        output = Recurrence(weight_dropped_lstm, go_backwards=go_backwards, initial_state=initial_state,
                            return_full_state=return_full_state)(x)

        # dropout applied outside of rnn as rnn hidden-to-hidden already regularised by dropconnect
        output = variational_dropout_output(output) if variational_dropout_output else output
        return output

    return _inject_name(inner, name)


def PositionwiseFeedForward(model_dim: int, intermediate_dim: int, dropout_rate: float = None,
                            intermediate_init=default_override_or(C.glorot_uniform()),  intermediate_init_bias=default_override_or(0),
                            init=default_override_or(C.glorot_uniform()),  init_bias=default_override_or(0),
                            name: str = ''):
    """ Implements Position-wise Feed-Forward Network found in Transformer and BERT

    For more details please refer to "Attention is all you need", https://arxiv.org/abs/1706.03762

    Arguments:
        model_dim (int): dimensionality of model (output)
        intermediate_dim (int): hidden/ intermediate dimension within layer
        dropout_rate (float): probability of dropping out an element
        intermediate_init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to :func:`~cntk.initializer.glorot_uniform` ): initial value of weights `W`
        intermediate_init_bias (scalar or NumPy array or :mod:`cntk.initializer`, defaults to 0): initial value of weights `b`
        init (scalar or NumPy array or :mod:`cntk.initializer`, defaults to :func:`~cntk.initializer.glorot_uniform` ): initial value of weights `W`
        init_bias (scalar or NumPy array or :mod:`cntk.initializer`, defaults to 0): initial value of weights `b`

    Returns:
        cntk.ops.functions.Function:
        A function that accepts one argument and applies the operation to it

    """
    inner_dense = Dense(intermediate_dim, init=intermediate_init, init_bias=intermediate_init_bias, name='intermediate')
    outer_dense = Dense(model_dim, init=init, init_bias=init_bias, name='dense')
    dropout = Dropout(dropout_rate)

    @C.BlockFunction('PositionwiseFeedForward', name)
    def inner(x):
        return outer_dense(dropout(C.relu(inner_dense(x))))

    return inner
