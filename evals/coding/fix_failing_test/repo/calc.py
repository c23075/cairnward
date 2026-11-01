"""A tiny module with a bug for agents to fix."""


def add(a: float, b: float) -> float:
    return a - b


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("mean of an empty list")
    return sum(values) / len(values)
