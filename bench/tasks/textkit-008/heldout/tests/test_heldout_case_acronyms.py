from textkit import camel_to_snake


def test_acronyms_stay_together():
    assert camel_to_snake("HTTPServerError") == "http_server_error"
    assert camel_to_snake("parseXMLDocument") == "parse_xml_document"
    assert camel_to_snake("getHTTPResponseCode") == "get_http_response_code"


def test_simple_names_unchanged():
    assert camel_to_snake("userName") == "user_name"
    assert camel_to_snake("userID") == "user_id"
