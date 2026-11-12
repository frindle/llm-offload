def get_setting(settings, name, default):
    value = settings.get(name)
    if value is None:
        return default
    return value
