# Bases pinned by digest (Dependabot proposes updates). CI's go job reads the Go version from
# the build stage's tag, so the tests run on the release that builds this binary.
FROM golang:1.25.14-alpine3.24@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/sandbox-svc ./cmd/sandbox-svc

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/sandbox-svc /sandbox-svc
ENV SANDBOX_ADDR=0.0.0.0:8090
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s CMD ["/sandbox-svc", "-healthcheck"]
ENTRYPOINT ["/sandbox-svc"]
