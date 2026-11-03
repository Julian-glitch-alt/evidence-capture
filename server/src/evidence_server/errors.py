class CaptureError(Exception):
    """A request the server refuses; the message is safe to show to the client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
