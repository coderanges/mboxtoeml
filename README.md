# mbox to eml

Convert email export format from `mbox` to `eml`, plus a few handy CLI helpers.

Tested on `python3.11` but should work fine on other versions of `python3`. Stdlib-only, no dependencies.

## Usage

### Web app (recommended)

```bash
python3 server.py --port 8000
# open http://127.0.0.1:8000/
```

Enter the absolute MBOX path and output directory on this machine, Inspect, then Start conversion.
Supports overwrite/skip/rename on collisions, live progress (SSE with polling fallback),
paginated results, sandboxed EML preview, and header modification.

### CLI tools

```bash
python3 mbox2eml.py -f spam.mbox -o output --collision overwrite
python3 read_eml.py <input_eml_path>
python3 modify_eml.py <input_eml_path> <output_eml_path> --header To --value user@example.com
```

## Verification

```bash
python3 -m unittest -v test_mail_tools.py test_server.py
```
