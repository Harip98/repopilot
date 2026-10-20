from textkit import camel_to_snake, snake_to_camel


def test_camel_to_snake_simple():
    assert camel_to_snake("userName") == "user_name"


def test_snake_to_camel():
    assert snake_to_camel("user_name") == "userName"
    assert snake_to_camel("user_name", upper_first=True) == "UserName"


def test_private_prefix_kept():
    assert snake_to_camel("_private_value") == "_privateValue"
