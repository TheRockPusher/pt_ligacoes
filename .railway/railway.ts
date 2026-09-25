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
      // Initially off; operators enable it only after protecting admin access.
      ENABLE_ADMIN: preserve(),
      // An absent/empty token disables the API until separately provisioned.
      IMPORT_API_TOKEN: preserve(),
      PORT: "8080",
    },
  });

  const importsWorker = service("imports-worker", {
    source: github("TheRockPusher/pt_ligacoes", { branch: "main", checkSuites: true }),
    build: { builder: "DOCKERFILE", dockerfilePath: "infra/Dockerfile" },
    // Web alone migrates. The worker refuses to claim jobs with pending migrations;
    // after a deployment race exhausts retries, restart it after web migration succeeds.
    start: "python apps/platform/manage.py run_import_worker",
    replicas: { ams: 1 },
    deploy: {
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 3,
      healthcheckPath: null,
      cronSchedule: null,
      sleepApplication: false,
    },
    domains: [],
    tcp: [],
    env: {
      APP_PROCESS: "import-worker",
      DJANGO_SETTINGS_MODULE: "config.settings.production",
      // Reference the restricted web role, never the Postgres superuser URL.
      DATABASE_URL: web.env.DATABASE_URL,
      SECRET_KEY: web.env.SECRET_KEY,
      ALLOWED_HOSTS: web.env.ALLOWED_HOSTS,
      CSRF_TRUSTED_ORIGINS: web.env.CSRF_TRUSTED_ORIGINS,
      ENABLE_ADMIN: "false",
    },
  });

  return project("pt-ligacoes", {
    environments: ["production"],
    resources: [web, importsWorker, postgres, postgresVolume],
  });
});
