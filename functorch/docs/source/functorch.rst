functorch
=========

.. currentmodule:: functorch

.. warning::

   We've integrated functorch into PyTorch. As the final step of the
   integration, the functorch APIs are deprecated as of PyTorch 2.0.
   Please use the torch.func APIs instead and see the
   `migration guide <https://pytorch.org/docs/main/func.migrating.html>`_
   and `docs <https://pytorch.org/docs/main/func.html>`_
   for more details.

Function Transforms
-------------------
.. autosummary::
    :toctree: generated
    :nosignatures:

    vmap
    grad
    grad_and_value
    vjp
    jvp
    jacrev
    jacfwd
    hessian
    functionalize

Utilities for working with torch.nn.Modules
-------------------------------------------

In general, you can transform over a function that calls a ``torch.nn.Module``.
For example, the following is an example of computing a jacobian of a function
that takes three values and returns three values:

.. code-block:: python

    model = torch.nn.Linear(3, 3)

    def f(x):
        return model(x)

    x = torch.randn(3)
    jacobian = jacrev(f)(x)
    assert jacobian.shape == (3, 3)

However, if you want to do something like compute a jacobian over the parameters
of the model, then there needs to be a way to construct a function where the
parameters are the inputs to the function.
That's what :func:`make_functional` and :func:`make_functional_with_buffers` are for:
given a ``torch.nn.Module``, these return a new function that accepts ``parameters``
and the inputs to the Module's forward pass.

.. autosummary::
    :toctree: generated
    :nosignatures:

    make_functional
    make_functional_with_buffers
    combine_state_for_ensemble

If you're looking for information on fixing Batch Norm modules, please follow the
guidance here

.. toctree::
   :maxdepth: 1

   batch_norm
