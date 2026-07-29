module.exports = {
  server: {
    host: "127.0.0.1",
    port: 8080,
    keepAliveTimeout: 5000,
  },
  logging: {
    level: "info",
    destination: "stdout",
    rotate: true,
  },
  features: {
    retries: true,
    tracing: false,
    experimentalCache: false,
  },
  limits: {
    maxBodyBytes: 1048576,
    maxConnections: 512,
  },
};
