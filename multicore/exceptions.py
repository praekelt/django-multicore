class MulticoreError(Exception):
    pass


class TimeoutExceededError(MulticoreError):
    pass


class NoAvailableInputBufferError(MulticoreError):
    pass


class InputBufferTooSmallError(MulticoreError):
    pass
