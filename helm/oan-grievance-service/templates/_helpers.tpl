{{- define "clamav.labels" -}}
app.kubernetes.io/name: oan-clamav
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
ClamAV resource name: <release>-clamav, so release `grievance` gives the Service `grievance-clamav`
that grievance_clamav_host points at.
*/}}
{{- define "clamav.fullname" -}}
{{- printf "%s-clamav" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
