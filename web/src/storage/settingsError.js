export async function settingsResponseError(response) {
  const payload = await response.json().catch(() => null);
  const detail = typeof payload?.detail === "string" ? payload.detail.trim() : "";
  if (response.status === 409) return new Error(detail || "设置已更新，请刷新后重试。");
  if ([400, 422].includes(response.status)) {
    return new Error(detail || "配置未保存，请核对模型名称、接口地址与功能选择。");
  }
  return new Error(detail || "设置服务暂不可用，请检查后端连接后重试。");
}
