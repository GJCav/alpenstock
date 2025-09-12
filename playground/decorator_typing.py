from typing import Callable, TypeVar, ParamSpec, Protocol, cast, Concatenate
import functools

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


def demo_func(x: int, y: int = 10) -> float:
    """This document should be preserved after decoration.

    Args:
        x (int): an integer
        y (int, optional): an integer. Defaults to 10.

    Returns:
        float: the result of the function
    """
    return x + y / 2.0


# 🎈 1. Decorator with Proper Type Hints
def typed_decorator(func: Callable[P, R]) -> Callable[P, R]:
    # Preserve the docstring, name, and other metadata of the original function
    @functools.wraps(func)
    def wrapper(
        *args: P.args, **kwargs: P.kwargs
    ) -> R:  # Preserve the input/output types
        return func(*args, **kwargs)

    return wrapper


# hover over `decorated_demo_func` to see the type hints are preserved
decorated_demo_func = typed_decorator(demo_func)


# 🎈 2. Argumented Decorator with Type Hints and Friendly Docstring
def retry(times: int = 5) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry `times` times if exception occurs.

    Args:
        times (int, optional): Number of retries. Defaults to 5.

    Returns:
        Callable[Callable[P, R], Callable[P, R]]: the decorated function
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exception = None
            for _ in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
            if last_exception is not None:
                raise last_exception
            raise RuntimeError("Unreachable code")

        # 🚩 Add a simple tag in the docstring. But sadly, common IDEs only show
        # the old docstring without this addition.
        wrapper.__doc__ = (
            wrapper.__doc__ or ""
        ).rstrip() + f"\n[Decorated with @retry(times={times})]"
        return wrapper

    return decorator


# hover over `retry_decorated_demo_func` to see the type hints are preserved but
# only the original docstring is shown in the IDEs.
retry_decorated_demo_func = retry(times=3)(demo_func)
print(retry_decorated_demo_func.__doc__)


# 🎈 3. Add More Arguments to the Function

# Optional 1: Use `Concatenate`. The limitation of `Concatenate` is that:
#   - Can only be used as the first argument of `Callable`.
#   - Extra positional arguments should be added before the ParamSpec.
#   - Cannot add extra keyword-only arguments.
#   - Argument names are not preserved, only types are shown.
def decorator_with_extra_arg(func: Callable[P, R]) -> Callable[Concatenate[str, P], R]:
    """Correct way: Use Concatenate to add str before P"""

    @functools.wraps(func)
    def wrapper(
        extra_arg: str, *args, **kwargs
    ):  # No need of typing here because it is defined in the return type outside
        print(f"Extra argument: {extra_arg}")
        return func(*args, **kwargs)

    wrapper.__doc__ = (
        wrapper.__doc__ or ""
    ).rstrip() + "\n[Decorated with extra `str` argument]"
    return wrapper


func = decorator_with_extra_arg(demo_func)


# Option 2: Use Protocol for more complex cases
class CallableWithExtraArg(Protocol[P, R]):
    def __call__(
        self,
        extra_arg: str,
        *args: P.args,
        extra_kwarg: str = "abc",  # Allow adding extra keyword arguments
        **kwargs: P.kwargs,
    ) -> R:
        """This overrides the docstring of the original function.
        """

def decorator_with_protocol(func: Callable[P, R]) -> CallableWithExtraArg[P, R]:
    """Most explicit but least flexible"""
    # @functools.wraps(func)  # This does not work well with Protocol
    def wrapper(extra_arg: str, *args, extra_kwarg: str = "abc", **kwargs):
        print(f"Extra argument: {extra_arg}")
        print(f"Extra kwarg: {extra_kwarg}")
        return func(*args, **kwargs)

    # Copy the docstring of the original function and add a tag
    wrapper.__doc__ = (
        func.__doc__ or ""
    ).rstrip() + "\n[Decorated with extra `str` argument and `extra_kwarg` keyword argument]"
    return wrapper

func = decorator_with_protocol(demo_func)
func.__call__
# `func.__call__` has the correct signature, but `func` itself does not. After
# `func(` is inputed, the IDEs will provide the correct signature help. However, 
# the docstring of `func` is lost in the completion helps of IDEs.
print(func.__doc__)


# 🎈 4. Removing Arguments from the Function
#
# The only possible way is to rewrite the signature manually. No perfect solution.

# 🎈 5. Extend a class by decorating
# 
# A simple class to be decorated
class DemoClass:
    """Class docstring should be preserved after decoration."""

    def demo_func(self, x: int) -> float:
        """This document should be preserved after decoration."""
        return x / 2.0


class ExtendMethod(Protocol):
    def extended_method(self, y: int) -> float: ...


# As of Python 3.14, type intersection is not supported yet. So `T &
# ExtendedMethod` is not valid. There is no perfect solution to this problem
# yet.

# def extend_class(cls: type[T]) -> type[T & ExtendedMethod]:
#     cls.extended_method = ExtendedMethod.extended_method
#     return cls


# We recommend using mixin-style as the workaround. Turn `ExtendedMethod` into a
# mixin class. And use it like this:
class ExtendMixin:
    def extended_method(self, y: int) -> float:
        """Extended method by the decorator

        Args:
            y (int): some integer

        Returns:
            float: the result of the function
        """
        return y * 2.1


class ExtendedDemoClass(DemoClass, ExtendMixin):
    pass


# But now, the docstring of `DemoClass` is lost.
b = ExtendedDemoClass()
