"""
A system for helping you separate your IO and state-manipulation code
(hereafter referred to as "effects") from everything else, thus allowing
the majority of your code to be trivially testable and composable (that is,
have the general benefits of purely functional code).

Keywords: monad, IO, stateless.

The core behavior is as follows:
- Effectful operations should be represented as plain Python objects which
  we will call the *intent* of an effect. These objects should be wrapped
  in an instance of :class:`Effect`.
- Intent objects can implement a 'perform_effect' method to actually perform
  the effect. This method should _not_ be called directly.
- In most cases where you'd perform effects in your code, you should instead
  return an Effect wrapping the effect intent.
- To represent work to be done *after* an effect, use Effect.on_success,
  .on_error, etc.
- Near the top level of your code, invoke Effect.perform on the Effect you
  have produced. This will invoke the effect-performing handler specific to
  the wrapped object, and invoke callbacks as necessary.
- If the callbacks return another instance of Effect, that Effect will be
  performed before continuing back down the callback chain.

On top of the implemented behaviors, there are some conventions that these
behaviors synergize with:

- Don't perform actual IO or manipulate state in functions that return an
  Effect. This kinda goes without saying. However, there are a few cases
  where you may decide to compromise:
  - most commonly, logging.
  - generation of random numbers.
- Effect-wrapped objects should be *inert* and *transparent*. That is, they
  should be unchanging data structures that fully describe the behavior to be
  performed with public members. This serves two purposes:
  - First, and most importantly, it makes unit-testing your code trivial.
    The tests will simply invoke the function, inspect the Effect-wrapped
    object for correctness, and manually resolve the effect to execute any
    callbacks in order to test the post-Effect behavior. No more need to mock
    out effects in your unit tests!
  - This allows the effect-code to be *small* and *replaceable*. Using these
    conventions, it's trivial to switch out the implementation of e.g. your
    HTTP client, using a blocking or non-blocking network library, or
    configure a threading policy. This is only possible if effect intents
    expose everything necessary via a public API to alternative
    implementation.
- To spell it out clearly: do not call Effect.perform() on Effects produced by
  your code under test: there's no point. Just grab the 'intent'
  attribute and look at its public attributes to determine if your code
  is producing the correct kind of effect intents. Separate unit tests for
  your effect *handlers* are the only tests that need concern themselves with
  true effects.
- When testing, use the utilities in the effect.testing module: they will help
  a lot.

Twisted's Deferreds are supported directly; any effect handler that returns
a Deferred will seamlessly integrate with on_success, on_error etc callbacks.

Support for AsyncIO tasks, and other callback-oriented frameworks, is to be
done, but should be trivial.

UNFORTUNATE:

- In general, callbacks should not need to care about the implementation
  of the effect handlers. However, currently error conditions are passed to
  error handlers in an implementation-bound way: when Deferreds are involved,
  Failures are passed, whereas when synchronous exceptions are raised, a
  sys.exc_info() tuple is passed. This should be fixed somehow, maybe by
  relying on a split-out version of Twisted's Failures.
- It's unclear whether the handler-table approach to effect dispatching is
  flexible enough for all/common cases. For example, a system which mixes
  asynchronous and synchronous IO (because multiple libraries that do things
  in different ways are both in use) won't have a way to differentiate an
  asynchronous HTTPRequest from a synchronous HTTPRequest in the same call to
  Effect.perform. Likewise, a threaded implementation of parallel should only
  be used when in the context of Deferred-returning effects.
- Maybe allowing intents to provide their own implementations of
  perform_effect is a bad idea; if users don't get used to constructing their
  own set of handlers, then when they need to customize an effect handler it
  may require an unfortunately large refactoring.

TODO:
- further consider "generic function" style dispatching to effect
  handlers. https://pypi.python.org/pypi/singledispatch
- consider rewriting callbacks to be an ordered list attached to the effect,
  instead of effect wrappers. This could help performance and reduce stack
  size, but more importantly it can simplify a lot of code in the testing
  module.
"""

from __future__ import print_function

import sys
from functools import partial


class NoEffectHandlerError(Exception):
    """
    No Effect handler could be found for the given Effect-wrapped object.
    """


class Effect(object):
    """
    Wrap an object that describes how to perform some effect (called an
    "effect intent"), and offer a way to actually perform that effect.

    (You're an object-oriented programmer.
     You probably want to subclass this.
     Don't.)
    """
    def __init__(self, intent):
        """
        :param intent: An object that describes an effect to be
            performed. Optionally has a perform_effect(handlers) method.
        """
        self.intent = intent
        self.callbacks = []

    @classmethod
    def with_callbacks(klass, intent, callbacks):
        eff = klass(intent)
        eff.callbacks = callbacks
        return eff

    def perform(self, handlers):
        """
        Perform an effect by dispatching to the appropriate handler.

        If the type of the effect intent is in ``handlers``, that handler
        will be invoked. Otherwise a ``perform_effect`` method will be invoked
        on the effect intent.

        If an effect handler returns another :class:`Effect` instance, that
        effect will be performed immediately before returning.

        :param handlers: A dictionary mapping types of effect intents
            to handler functions.
        :raise NoEffectHandlerError: When no handler was found for the effect
            intent.
        """
        func = None
        if type(self.intent) in handlers:
            func = partial(handlers[type(self.intent)], self.intent)
        if func is None:
            func = getattr(self.intent, 'perform_effect', None)
        if func is None:
            raise NoEffectHandlerError(self.intent)

        return self._dispatch_callback_chain(self.callbacks, func, handlers)

    def _maybe_chain(self, result, handlers):
        # Not happy about this Twisted knowledge being in Effect...
        if hasattr(result, 'addCallback'):
            return result.addCallback(self._maybe_chain, handlers)
        if type(result) is Effect:
            return result.perform(handlers)
        return result

    def _dispatch_callback_chain(self, chain, init_func, handlers):
        result = handlers
        is_error = False
        for callback_index, (success, error) in enumerate([(init_func, None)]
                                                          + self.callbacks):
            cb = success if not is_error else error
            if cb is None:
                continue
            is_error, result = self._dispatch_callback(cb, result)
            result = self._maybe_chain(result, handlers)
            if hasattr(result, 'addCallbacks'):
                # short circuit all the rest of the callbacks; they become
                # callbacks on the Deferred instead of the effect.
                return self._chain_deferred(result,
                                            self.callbacks[callback_index:])
        if is_error:
            raise result[1:]
        return result

    def _chain_deferred(self, deferred, callbacks):
        for cb, eb in callbacks:
            if cb is None:
                cb = lambda r: r
            deferred.addCallbacks(cb, eb)
        return deferred

    def _dispatch_callback(self, callback, argument):
        try:
            return (False, callback(argument))
        except:
            return (True, sys.exc_info())

    def on_success(self, callback):
        """
        Return a new Effect that will invoke the associated callback when this
        Effect completes succesfully.
        """
        return self.on(success=callback, error=None)

    def on_error(self, callback):
        """
        Return a new Effect that will invoke the associated callback when this
        Effect fails.

        The callback will be invoked with the sys.exc_info() exception tuple
        as its only argument.
        """
        return self.on(success=None, error=callback)

    def after(self, callback):
        """
        Return a new Effect that will invoke the associated callback when this
        Effect completes, whether successfully or in error.
        """
        return self.on(success=callback, error=callback)

    def on(self, success, error):
        """
        Return a new Effect that will invoke either the success or error
        callbacks provided based on whether this Effect completes sucessfully
        or in error.
        """
        return Effect.with_callbacks(self.intent, self.callbacks + [(success, error)])

    def __repr__(self):
        return "Effect.with_callbacks(%r, %s)" % (self.intent, self.callbacks)

    def serialize(self):
        """
        A simple debugging tool that serializes a tree of effects into basic
        Python data structures that are useful for pretty-printing.

        If the effect intent has a "serialize" method, it will be invoked to
        represent itself in the resulting structure.
        """
        if hasattr(self.intent, 'serialize'):
            intent = self.intent.serialize()
        else:
            intent = self.intent
        return {"type": type(self), "intent": intent, "callbacks": self.callbacks}


class ParallelEffects(object):
    """
    An effect intent that asks for a number of effects to be run in parallel,
    and for their results to be gathered up into a sequence.

    The default implementation of this effect relies on Twisted's Deferreds.
    An alternative implementation can run the child effects in threads, for
    example. Of course, the implementation strategy for this effect will need
    to cooperate with the effects being parallelized -- there's not much use
    running a Deferred-returning effect in a thread.
    """
    def __init__(self, effects):
        self.effects = effects

    def __repr__(self):
        return "ParallelEffects(%r)" % (self.effects,)

    def serialize(self):
        return {"type": type(self),
                "effects": [e.serialize() for e in self.effects]}

    def perform_effect(self, handlers):
        from twisted.internet.defer import gatherResults, maybeDeferred
        return gatherResults(
            [maybeDeferred(e.perform, handlers) for e in self.effects])


def parallel(effects):
    """
    Given multiple Effects, return one Effect that represents the aggregate of
    all of their effects.
    The result of the aggregate Effect will be a list of their results, in
    the same order as the input to this function.
    """
    return Effect(ParallelEffects(effects))
