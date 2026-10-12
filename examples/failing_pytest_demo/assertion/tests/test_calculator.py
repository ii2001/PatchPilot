from calculator import add


def test_add():
    assert add(2, 3) == 5
    assert add(-2, 3) == 1
