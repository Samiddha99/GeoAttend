class ObjectIdConverter:
    """MongoDB primary keys in URLs: 24 hex characters."""
    regex = "[0-9a-fA-F]{24}"

    def to_python(self, value):
        return value          # ObjectIdAutoField coerces the string in queries

    def to_url(self, value):
        return str(value)     # so reverse() accepts a real ObjectId