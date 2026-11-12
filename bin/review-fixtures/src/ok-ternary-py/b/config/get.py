def get_setting(settings, name, default):
    value = settings.get(name)
    return default if value is None else value
