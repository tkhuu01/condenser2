from typing import Any, Final

from faker import Faker

fake = Faker()


class DataMasking:
    @staticmethod
    def null_out(_: str):
        return None

    @staticmethod
    def mask_numbers(value: Any) -> str | None:
        """
        Mask certain strings that may contain a mixture of letters,
        normal characters, whitespaces, or special characters
        """
        if value is None:
            return None
        str_value = str(value)
        return "".join(
            str(fake.random_digit()) if c.isdigit() else c for c in str_value
        )

    @staticmethod
    def mask_characters(value: Any) -> str | None:
        """
        Mask certain strings that may contain a mixture of letters,
        normal characters, whitespaces, or special characters
        """
        if value is None:
            return None
        str_value = str(value)
        return "".join(fake.random_letter() if c.isalpha() else c for c in str_value)

    @staticmethod
    def mask_email(email: Any) -> str | None:
        if email is None:
            return None
        s = str(email).split("@")
        if len(s) < 2:
            return fake.email()
        return f"{fake.user_name()}@{s[1]}"


DATA_MASKING_MAPPER: Final = {
    "null_out": DataMasking.null_out,
    "mask_numbers": DataMasking.mask_numbers,
    "mask_characters": DataMasking.mask_characters,
    "mask_email": DataMasking.mask_email,
}
