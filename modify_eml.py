import argparse
import re
import sys
from email import policy
from email.generator import BytesGenerator
from email.parser import BytesParser

_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def validate_header_name(header_name):
    if not isinstance(header_name, str) or not header_name:
        raise ValueError("Header name must be a non-empty string")
    if len(header_name) > 78:
        raise ValueError("Header name too long (max 78 characters)")
    if not _HEADER_NAME_RE.match(header_name):
        raise ValueError(f"Invalid header name: {header_name!r} (use letters, digits, hyphens)")
    return header_name


def validate_header_value(header_value):
    if not isinstance(header_value, str):
        raise ValueError("Header value must be a string")
    if "\r" in header_value or "\n" in header_value or "\x00" in header_value:
        raise ValueError("Header value must not contain CR, LF, or NUL (header injection)")
    if len(header_value) > 998:
        raise ValueError("Header value too long (max 998 characters)")
    return header_value


def set_header(message, header_name, header_value):
    """Replace an existing header or add it if missing."""
    validate_header_name(header_name)
    validate_header_value(header_value)
    if header_name in message:
        message.replace_header(header_name, header_value)
        return

    message[header_name] = header_value


def read_and_modify_eml(input_eml, output_eml, header_name="To", header_value="user@example.com"):
    validate_header_name(header_name)
    validate_header_value(header_value)
    # Parse the input EML file
    with open(input_eml, "rb") as eml_data:
        msg = BytesParser(policy=policy.default).parse(eml_data)

    set_header(msg, header_name, header_value)

    # Write the modified content to the new EML file
    with open(output_eml, "wb") as new_eml:
        generator = BytesGenerator(new_eml)
        generator.flatten(msg)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Replace or add an EML header")
    parser.add_argument("input_eml_path", help="Path to the input EML file")
    parser.add_argument("output_eml_path", help="Path to the output EML file")
    parser.add_argument(
        "--header",
        default="To",
        help="Header name to replace or add. Default: To",
    )
    parser.add_argument(
        "--value",
        default="user@example.com",
        help="Header value to write. Default: user@example.com",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    try:
        read_and_modify_eml(
            args.input_eml_path,
            args.output_eml_path,
            header_name=args.header,
            header_value=args.value,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
    print(f"Modified content written to {args.output_eml_path}")
