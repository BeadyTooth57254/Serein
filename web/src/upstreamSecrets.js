export function upstreamsForSave(upstreams, form) {
  const inputValues = new Map(
    Array.from(form?.querySelectorAll?.("[data-upstream-api-key]") || [], input => [
      input.dataset.upstreamId,
      input.value,
    ]),
  );
  return upstreams.map(({api_key_configured,clear_key,...upstream}) => {
    const apiKey = inputValues.has(upstream.id) ? inputValues.get(upstream.id) : upstream.api_key;
    if (apiKey) {
      upstream.api_key = apiKey;
    } else if (clear_key) {
      upstream.api_key = "";
      upstream.api_key_env = "";
    } else {
      delete upstream.api_key;
    }
    return upstream;
  });
}
