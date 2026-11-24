"""Errors are described by their real cause, without secrets or payload values."""

from sqlalchemy.exc import DBAPIError

from ecommerce_ai.infrastructure.errors import (
    MAX_MESSAGE_LENGTH,
    error_fields,
    error_message,
    error_name,
    root_error,
)


class InvalidPasswordError(Exception):
    """Stands in for ``asyncpg.exceptions.InvalidPasswordError``."""


def _wrapped(driver_error: Exception) -> DBAPIError:
    """The chain SQLAlchemy builds over asyncpg: DBAPIError -> adapter Error -> driver error."""
    adapter = Exception(str(driver_error))
    adapter.__cause__ = driver_error
    return DBAPIError("SELECT 1", None, adapter)


def test_should_name_the_driver_error_when_sqlalchemy_wraps_it_in_dbapierror() -> None:
    error = _wrapped(InvalidPasswordError('password authentication failed for user "joaof"'))

    assert error_name(error) == "InvalidPasswordError"
    assert error_message(error) == 'password authentication failed for user "joaof"'
    assert isinstance(root_error(error), InvalidPasswordError)


def test_should_keep_an_error_that_is_not_a_database_error_as_it_is() -> None:
    error = RuntimeError("kafka unreachable")
    error.__cause__ = OSError("connection refused")

    assert error_fields(error) == {"error": "RuntimeError", "error_message": "kafka unreachable"}


def test_should_keep_the_dbapierror_when_it_wraps_nothing() -> None:
    error = DBAPIError("SELECT 1", None, None)  # type: ignore[arg-type]  # no driver error at all

    assert error_name(error) == "DBAPIError"


def test_should_scrub_passwords_from_the_message_when_it_echoes_a_url() -> None:
    error = OSError("cannot reach postgresql://ai_user:s3cret@db:5436/ecommerce_ai")

    message = error_message(error)

    assert "s3cret" not in message
    assert "ai_user" in message


def test_should_drop_query_argument_values_when_the_driver_echoes_them() -> None:
    driver_error = Exception(
        "invalid input for query argument $3: 'Ana Silva, card 4111' (value too long)"
    )

    message = error_message(_wrapped(driver_error))

    assert message == "invalid input for query argument $3: [REDACTED]"


def test_should_keep_one_short_line_when_the_message_is_long_and_multiline() -> None:
    message = error_message(ValueError("first line\n" + "x" * 1000))

    assert "\n" not in message
    assert message.startswith("first line x")
    assert len(message) == MAX_MESSAGE_LENGTH
