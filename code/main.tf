terraform {
  required_version = ">=1.3"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.80.0"
    }
    google-beta = "~> 3.9"
  }
}

#reference to our project build with /cicd
data "google_project" "target" {
  project_id = var.project_id
}

locals {
  project_id           = data.google_project.target.project_id
  function_bucket_name = "bkt-function-${local.project_id}"
  cloudbuild_sa        = "serviceAccount:${data.google_project.target.number}@cloudbuild.gserviceaccount.com"
  gar_repo_name        = format("%s-%s", "prj", "containers") #container artifact registry repository
  service_name         = var.service_name
  location             = "us-central1"

  # services particular to this cloud run function
  services = ["cloudfunctions.googleapis.com",
    "secretmanager.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "pubsub.googleapis.com",
  "aiplatform.googleapis.com"]
}


# enable services used by the bot function
resource "google_project_service" "services" {
  for_each           = toset(local.services)
  project            = data.google_project.target.project_id
  service            = each.value
  disable_on_destroy = false
}

# dedicated service account for our cloudrun service
# so we don't use the default compute engine service account
resource "google_service_account" "cloudrun_service_identity" {
  project    = local.project_id
  account_id = "${local.service_name}-svc-account"
}


# secrets for the bot function
resource "google_secret_manager_secret" "slack_bot_token" {
  secret_id = "slack_bot_token"
  project   = local.project_id

  labels = {
    label = "secret-slack-bot-token"
  }

  replication {
    user_managed {
      replicas {
        location = var.default_region
      }
    }
  }
  depends_on = [
    google_project_service.services
  ]
}

# value for the slack_bot_token should be set via the console (search secret manager)


# signing secret
resource "google_secret_manager_secret" "slack_signing_secret" {
  secret_id = "slack_signing_secret"
  project   = local.project_id

  labels = {
    label = "secret-slack-signing-secret"
  }

  replication {
    user_managed {
      replicas {
        location = var.default_region
      }
    }
  }
  depends_on = [
    google_project_service.services
  ]
}

# value for the slack_signing_secret should be set via the console (search secret manager)


# allow the  service account to access the secrets
resource "google_project_iam_member" "secret_access" {
  provider = google-beta
  project  = local.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.cloudrun_service_identity.email}"
}

# allow the  service account to access AI
resource "google_project_iam_member" "ai_access" {
  provider = google-beta
  project  = local.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.cloudrun_service_identity.email}"
}

/**
cloud build container
**/

resource "null_resource" "cloudbuild_cloudrun_container" {
  # build if source changes
  triggers = {
    dir_sha1 = sha1(join("", [for f in fileset(path.root, "source/**") : filesha1(f)]))
  }


  provisioner "local-exec" {
    command = <<EOT
      gcloud builds submit ./source/ --project ${local.project_id} --config=./source/cloudbuild.yaml --substitutions=_SERVICE_NAME=${local.service_name}
  EOT
  }
}


# set a project policy to allow allUsers invoke
resource "google_project_organization_policy" "services_policy" {
  project    = local.project_id
  constraint = "iam.allowedPolicyMemberDomains"

  list_policy {
    allow {
      all = true
    }
  }
}

resource "google_cloud_run_service" "default" {
  name                       = local.service_name
  location                   = local.location
  project                    = local.project_id
  autogenerate_revision_name = true

  template {
    spec {
      service_account_name = google_service_account.cloudrun_service_identity.email
      containers {
        image = "${local.location}-docker.pkg.dev/${local.project_id}/${local.gar_repo_name}/${local.service_name}"
        env {
          name  = "PROJECT_ID"
          value = local.project_id
        }
      }
    }
  }

}

data "google_iam_policy" "noauth" {
  binding {
    role = "roles/run.invoker"
    members = [
      "allUsers",
    ]
  }
}

resource "google_cloud_run_service_iam_policy" "noauth" {
  location = google_cloud_run_service.default.location
  project  = local.project_id
  service  = google_cloud_run_service.default.name

  policy_data = data.google_iam_policy.noauth.policy_data
}


# pubsub setup
resource "google_service_account" "sa_pubsub" {
  account_id   = "${local.service_name}-sa-pubsub"
  display_name = "Cloud Run Pub/Sub invoker"
  project      = local.project_id

}

# allow cloudbuild to set pubsub topics / subscriptions
resource "google_pubsub_topic_iam_member" "cloudbuild" {
  role    = "roles/pubsub.admin"
  project = local.project_id
  topic   = google_pubsub_topic.slack-messages.name
  member  = local.cloudbuild_sa

}

# topic
resource "google_pubsub_topic" "slack-messages" {
  name    = "slack-messages"
  project = local.project_id
}



# subscription
resource "google_pubsub_subscription" "subscription" {
  name    = "${google_pubsub_topic.slack-messages.name}-subscription"
  topic   = google_pubsub_topic.slack-messages.name
  project = local.project_id

  ack_deadline_seconds = 360
  push_config {
    push_endpoint = google_cloud_run_service.default.status[0].url
    oidc_token {
      service_account_email = google_service_account.cloudrun_service_identity.email
    }
    attributes = {
      x-goog-version = "v1"
    }
  }

  depends_on = [google_cloud_run_service.default]

}

# allow us to publish and subscribe to our topic
# as we send and consume our own messages
resource "google_pubsub_subscription_iam_member" "subscribe" {
  subscription = google_pubsub_subscription.subscription.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.cloudrun_service_identity.email}"
  project      = local.project_id

}

resource "google_pubsub_topic_iam_member" "publish" {
  topic   = google_pubsub_topic.slack-messages.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.cloudrun_service_identity.email}"
  project = local.project_id
}
