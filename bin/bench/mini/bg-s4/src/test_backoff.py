from backoff import label


def test_label():
    assert label(2) == 'retry-2'
