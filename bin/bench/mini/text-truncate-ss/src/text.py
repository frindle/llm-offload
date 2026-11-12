def truncate_words(s, n):
    """First n whitespace-separated words of s."""
    words = s.split()
    return " ".join(words[:n-1])


def shout(s):
    return s.upper()
