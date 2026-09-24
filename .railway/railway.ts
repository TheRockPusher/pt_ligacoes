import { defineRailway, github, image, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const postgresVolume = volume("postgres-volume", {
    region: "ams",
    sizeMB: 50000,
    allowOnlineResize: true,
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
  });

  // Keep the existing template service. The database helper provisions public TCP
  // access and generated credentials; neither belongs in this private deployment.
  const postgres = service("Postgres", {
    source: image("ghcr.io/railwayapp-templates/postgres-ssl:18"),
    replicas: { ams: 1 },
    deploy: { requiredMountPath: "/var/lib/postgresql/data" },
    networking: { privateNetworkEndpoint: "postgres" },
    domains: [],
    tcp: [],
    volumeMounts: { "/var/lib/postgresql/data": postgresVolume },
    env: {
      DATABASE_URL: preserve(),
      PGDATA: preserve(),
      PGDATABASE: preserve(),
      PGHOST: preserve(),
      PGPASSWORD: preserve(),
      PGPORT: preserve(),
      PGUSER: preserve(),
      POSTGRES_DB: preserve(),
      POSTGRES_PASSWORD: preserve(),
      POSTGRES_USER: preserve(),
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: preserve(),
      SSL_CERT_DAYS: preserve(),
    },
  });

  const web = service("web", {
    source: github("TheRockPusher/pt_ligacoes", { branch: "main", checkSuites: true }),
    build: { builder: "DOCKERFILE", dockerfilePath: "infra/Dockerfile" },
    preDeploy: "timeout --kill-after=5s 300s python apps/platform/manage.py migrate --noinput",
    start: "sh infra/start.sh",
    healthcheck: "/healthz/",
    healthcheckTimeout: 120,
    replicas: { ams: 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 3 },
    env: {
      // This is a restricted application-role URL, not Postgres.DATABASE_URL.
      DATABASE_URL: preserve(),
      SECRET_KEY: preserve(),
      ALLOWED_HOSTS: preserve(),
      CSRF_TRUSTED_ORIGINS: preserve(),
      ENABLE_ADMIN: "false",
      PORT: "8080",
    },
  });

  return project("pt-ligacoes", {
    environments: ["production"],
    resources: [web, postgres, postgresVolume],
  });
});
