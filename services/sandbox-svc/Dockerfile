FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/sandbox-svc ./cmd/sandbox-svc

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/sandbox-svc /sandbox-svc
ENV SANDBOX_ADDR=0.0.0.0:8090
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s CMD ["/sandbox-svc", "-healthcheck"]
ENTRYPOINT ["/sandbox-svc"]
