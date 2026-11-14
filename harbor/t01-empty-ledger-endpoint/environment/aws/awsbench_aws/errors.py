class AwsError(Exception):
    """An AWS-shaped error: its code and message, as botocore's ClientError carries them."""

    def __init__(self, code: str, message: str = "", status: int = 400):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status

    def as_dict(self) -> dict:
        return {"Code": self.code, "Message": self.message}
