# Web container

Build the standalone Next.js image from the repository root:

```sh
docker build -f docker/Dockerfile.web -t x-reach-web .
```

Run it on port 3000:

```sh
docker run --rm --name x-reach-web -p 3000:3000 x-reach-web
```

The container listens on `0.0.0.0:3000`; override `PORT` to select another
port. The image serves the standalone Next.js build and its static assets as
an unprivileged user. The UI requests `/api/v1/*` on its own origin, so route
those paths to the API service at the deployment gateway or reverse proxy.
Docker's health check requests `/` and reports the container healthy when the
web server responds successfully.
