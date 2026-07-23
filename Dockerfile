# TODO: choose a base image.
#   - Use a Python 3.11 image, "slim" variant recommended (smaller, fewer
#     packages than the default image).

# TODO: set the working directory for the rest of the instructions below
#   (e.g. /app).

# TODO: install dependencies.
#   - Copy ONLY requirements.txt first, then run pip install.
#   - Copying requirements.txt before the rest of the source means Docker
#     can reuse this layer from cache when only your source code changes,
#     instead of reinstalling every dependency on every build.
#   - Use `pip install --no-cache-dir -r requirements.txt` to avoid
#     bloating the image with pip's download cache.

# TODO: copy the rest of the service source code into the image.

# TODO: document the port this service listens on.
#   - This is metadata only — it does not publish the port. Publishing
#     happens in docker-compose.yml.

# TODO: define the container's start command.
#   - Run with uvicorn: `uvicorn main:app --host 0.0.0.0 --port 8000`
#   - Binding to 0.0.0.0 (not 127.0.0.1) is required — otherwise the
#     service is unreachable from other containers on the network.
