export function upstreamModels(upstreams) {
  return upstreams.flatMap(upstream=>{
    const models=(upstream.models || []).map(item=>typeof item==='string'?{id:item,upstream_model:item}:item);
    if(upstream.default_model && !models.some(item=>item.id===upstream.default_model)) {
      models.push({id:upstream.default_model,upstream_model:upstream.default_model});
    }
    return models.filter(item=>item.id).map(item=>({...item,model:item.upstream_model,label:item.label || item.upstream_model,
      protocol:upstream.protocol,upstream_name:upstream.name}));
  });
}

export function taskModelOptions(models, assignments, task) {
  return models.filter(model=>['embedding','reranker'].includes(task)
    ? model.protocol==='openai'
    : ![assignments.embedding,assignments.reranker].includes(model.id));
}
