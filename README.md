# agentify-joel

## Setup

This project requires a virtual environment to manage dependencies.

```bash
# Ensure the system has the necessary venv tools (Debian/Ubuntu)
sudo apt install python3-full

python3 -m venv .venv

# On Linux/macOS:
. .venv/bin/activate 

# Ensure you have a pyproject.toml in this directory.
# Then, install the build tool:
pip install build

# Build the project:
python3 -m build

# Alternative: Run pip/python directly without activating:
# ./.venv/bin/python -m pip install build
```