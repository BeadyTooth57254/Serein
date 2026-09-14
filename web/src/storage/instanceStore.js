import { readLocalPreference, storeLocalPreference } from "./awakeStore.js";

export function identityName(role) {
  return role === "user"
    ? readLocalPreference("serein.awake.name.user", "User")
    : readLocalPreference("serein.awake.name.assistant", "AI");
}

export async function instanceSettings(changes) {
  const response = await fetch("/__serein/settings", {
    method: changes ? "PATCH" : "GET",
    headers: changes ? { "Content-Type": "application/json" } : {},
    ...(changes ? { body: JSON.stringify(changes) } : {}),
  });
  if (!response.ok) {
    if (response.status === 409) {const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:"设置已更新，请刷新后重试。");}
    if ([400,422].includes(response.status)) throw new Error("配置未保存，请核对模型名称、接口地址与功能选择。");
    throw new Error("设置服务暂不可用，请检查后端连接后重试。");
  }
  const result = await response.json();
  storeLocalPreference("serein.awake.name.user", result.identity.user_name);
  storeLocalPreference("serein.awake.name.assistant", result.identity.ai_name);
  storeLocalPreference("serein.awake.meetingDate", result.identity.meeting_date ?? "");
  window.dispatchEvent(new CustomEvent('serein:features',{detail:result.features}));
  if(changes)window.dispatchEvent(new CustomEvent('serein:settings-saved',{detail:result}));
  return result;
}
