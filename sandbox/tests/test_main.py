import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import add, greet


def test_add_integers():
    assert add(2, 3) == 5

def test_add_negatives():
    assert add(-1, 1) == 0

def test_add_floats():
    assert add(0.5, 0.5) == 1.0

def test_greet():
    assert greet("World") == "Hello, World!"

def test_greet_contains_name():
    assert "Nathan" in greet("Nathan")
