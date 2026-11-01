import pytest
from calc import add, mean


def test_add() -> None:
    assert add(2, 3) == 5


def test_mean() -> None:
    assert mean([1, 2, 3]) == 2


def test_mean_empty() -> None:
    with pytest.raises(ValueError):
        mean([])
