#!/bin/bash
# generate-tiered-cache-lambda-layer.sh

# Build the Docker image
docker build $DOCKER_NETWORK -t tiered-cache-lambda-layer-builder .

# Run the container and copy the zip file
docker run --rm \
  -v $(pwd)/../../resources2/artifacts:/layer/artifacts \
  tiered-cache-lambda-layer-builder \
  bash -c "chmod +x build-layer.sh && ./build-layer.sh && cp tiered-cache-lambda-layer.zip /layer/artifacts/"

echo "Lambda layer zip file has been created in the artifacts directory"
