output "cloudrun_url" {
  description = "cloud run service url"
  value       = google_cloud_run_service.default.status[0].url
}
