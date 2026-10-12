{{- define "weipai.name" -}}
{{- printf "%s-ops" .Release.Name | trunc 45 | trimSuffix "-" -}}
{{- end -}}
{{- define "weipai.labels" -}}
app.kubernetes.io/part-of: weipai-ops
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "weipai.backendEnv" -}}
- name: APP_ENV
  value: production
- name: OPS_READ_ONLY
  value: "true"
- name: WORKER_MODE
  value: readonly_discovery
- name: CONNECTOR_MODE
  value: real
- name: EXECUTION_CONFIG
  value: '{"enabled":false,"bindings":[]}'
- name: AGENT_CONFIG
  value: '{"enabled":false}'
- name: DATABASE_URL
  valueFrom:
    secretKeyRef: {name: {{ .Values.databaseSecret | quote }}, key: DATABASE_URL}
- name: TEMPORAL_CONFIG
  value: {{ dict "address" .Values.temporalAddress "namespace" .Values.temporalNamespace "task_queue" .Values.taskQueue | toJson | quote }}
{{- end -}}
{{- define "weipai.security" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: [ALL]}
{{- end -}}
