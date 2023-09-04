# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ================== Triggers for "prod" environment ==================

resource "google_cloudbuild_trigger" "validate_prod" {
  project     = google_project.cicd.project_id
  name        = "tf-validate-prod"
  description = "Terraform validate job triggered on push event."

  # if needed, trigger on only specific file changes.
  # included_files = [
  #   "./**",
  # ]

  trigger_template {
    repo_name   = local.repo_name
    branch_name = "^main$"
  }

  filename = "cicd/configs/tf-validate.yaml"

  substitutions = {
    _TERRAFORM_ROOT = "."
    _MANAGED_DIRS   = "cicd code"
  }

  depends_on = [
    google_project_service.services,
    google_sourcerepo_repository.configs,
  ]
}

resource "google_cloudbuild_trigger" "plan_prod" {
  project     = google_project.cicd.project_id
  name        = "tf-plan-prod"
  description = "Terraform plan job triggered on push event."

  # if needed, trigger on only specific file changes.
  # included_files = [
  #   "./**",
  # ]

  trigger_template {
    repo_name   = local.repo_name
    branch_name = "^main$"
  }

  filename = "cicd/configs/tf-plan.yaml"

  substitutions = {
    _TERRAFORM_ROOT = "."
    _MANAGED_DIRS   = "cicd code"
  }

  depends_on = [
    google_project_service.services,
    google_sourcerepo_repository.configs,
  ]
}


resource "google_cloudbuild_trigger" "apply_prod" {
  disabled    = true
  project     = google_project.cicd.project_id
  name        = "tf-apply-prod"
  description = "Terraform apply job triggered on push event and/or schedule."

  # if needed, trigger on only specific file changes.
  # included_files = [
  #   "./**",
  # ]

  trigger_template {
    repo_name   = local.repo_name
    branch_name = "^main$"
  }

  filename = "cicd/configs/tf-apply.yaml"

  substitutions = {
    _TERRAFORM_ROOT = "."
    _MANAGED_DIRS   = "cicd code"
  }

  depends_on = [
    google_project_service.services,
    google_sourcerepo_repository.configs,
  ]
}


/**
cloud build 'cloudbuilder' container
**/

resource "null_resource" "cloudbuild_terraform_builder" {
  triggers = {
    project_id_cloudbuild_project = google_project.cicd.project_id
    gar_name                      = local.repo_name
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud builds submit ./container/ --project ${google_project.cicd.project_id} --config=./container/cloudbuild.yaml
  EOT
  }
  depends_on = [
    google_artifact_registry_repository_iam_member.terraform-image-iam
  ]
}
