const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const SECRET_KEY='cosji_civit_pairing';
const LIMIT=6*1024*1024;
function targetUrl(value){const u=new URL(String(value));if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw Error('请输入有效的 ComfyUI 地址');return u.href.replace(/\/$/,'');}
async function readResponse(response){
    const chunks=[];let size=0;
    for await(const chunk of response.body){size+=chunk.length;if(size>LIMIT)throw Error('目标响应过大');chunks.push(Buffer.from(chunk));}
    let data;try{data=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Error('目标未返回有效 JSON（HTTP '+response.status+'）');}
    if(!response.ok)throw Error(data.error||'目标请求失败（HTTP '+response.status+'）');
    return data;
}
class Broker{
    constructor({secrets,fetchImpl=fetch}){this.secrets=secrets;this.fetch=fetchImpl;}
    file(dirs){return path.join(dirs.root,'cosji-civit-connections.json');}
    records(dirs){const f=this.file(dirs);return fs.existsSync(f)?JSON.parse(fs.readFileSync(f,'utf8')):[];}
    list(dirs){return this.records(dirs).map(({secretId,...publicRecord})=>publicRecord);}
    save(dirs,records){const f=this.file(dirs),temp=f+'.tmp';fs.writeFileSync(temp,JSON.stringify(records,null,2),{mode:0o600,flag:'wx'});fs.renameSync(temp,f);}
    async remote(url,key,route,body){
        const response=await this.fetch(url+'/cosji-civit/'+route,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+key,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),redirect:'manual',signal:AbortSignal.timeout(45000)});
        return readResponse(response);
    }
    async pair(dirs,{url,key,name}){
        url=targetUrl(url);if(typeof key!=='string'||key.length<32||key.length>256)throw Error('配对密钥格式无效');
        const info=await this.remote(url,key,'capabilities');if(info.protocol!==1)throw Error('目标下载插件协议不兼容，请更新插件');
        const records=this.records(dirs),existing=records.find(r=>r.url===url);
        const secretId=this.secrets.writeSecret(dirs,SECRET_KEY,key);
        const record={id:existing?.id||crypto.randomUUID(),name:String(name||url).slice(0,120),url,secretId};
        if(existing)Object.assign(existing,record);else records.push(record);
        this.save(dirs,records);return {id:record.id,name:record.name,url,info};
    }
    async proxy(dirs,{id,route,body}){
        if(typeof route!=='string'||!(/^(capabilities|jobs|inspect|download|jobs\/[a-f0-9]{32}(\/(cancel|retry))?)$/.test(route)))throw Error('不允许此代理操作');
        const record=this.records(dirs).find(r=>r.id===id);if(!record)throw Error('目标电脑尚未配对');
        const key=this.secrets.readSecret(dirs,SECRET_KEY,record.secretId);if(!key)throw Error('配对凭据已丢失，请重新配对');
        const isPost=['inspect','download'].includes(route)||route.endsWith('/cancel')||route.endsWith('/retry');
        if(isPost&&(body===null||typeof body!=='object'||Array.isArray(body)))throw Error('任务请求格式无效');
        return this.remote(record.url,key,route,isPost?body:undefined);
    }
    async wiki(search){
        if(typeof search!=='string'||!search.trim()||search.length>120)throw Error('作品搜索名称无效');
        const url=new URL('https://www.wikidata.org/w/api.php');
        url.search=new URLSearchParams({action:'wbsearchentities',format:'json',language:'zh',uselang:'zh',type:'item',limit:'5',search:search.trim()}).toString();
        const response=await this.fetch(url,{signal:AbortSignal.timeout(15000),redirect:'error',headers:{'User-Agent':'CosjiCivit/1.0 (public work-name lookup)'}});
        const data=await readResponse(response);
        return (data.search||[]).map(item=>({id:item.id,name:item.label,description:item.description||'',url:'https://www.wikidata.org/wiki/'+item.id}));
    }
    async preview(value){
        const url=new URL(String(value));if(url.protocol!=='https:'||url.hostname!=='image.civitai.com'||url.username||url.password)throw Error('只允许 Civitai 官方预览图');
        const r=await this.fetch(url,{redirect:'error',signal:AbortSignal.timeout(20000)});
        const mime=(r.headers.get('content-type')||'').split(';')[0];
        if(!r.ok||!['image/png','image/jpeg','image/webp'].includes(mime))throw Error('预览图不可读取或格式不支持');
        const chunks=[];let bytes=0;for await(const chunk of r.body){bytes+=chunk.length;if(bytes>8*1024*1024)throw Error('预览图超过 8 MiB');chunks.push(Buffer.from(chunk));}
        return {dataUrl:'data:'+mime+';base64,'+Buffer.concat(chunks).toString('base64')};
    }
    importFile(dirs){return path.join(dirs.root,'cosji-civit-imports.json');}
    imports(dirs){const f=this.importFile(dirs);return fs.existsSync(f)?JSON.parse(fs.readFileSync(f,'utf8')):[];}
    saveImport(dirs,record){
        if(!record||typeof record!=='object'||typeof record.id!=='string'||!/^[a-f0-9-]{16,80}$/.test(record.id)||typeof record.connectionId!=='string'||!this.records(dirs).some(r=>r.id===record.connectionId))throw Error('导入任务格式或目标连接无效');
        const text=JSON.stringify(record);if(text.length>160000)throw Error('导入资料过大');
        const forbidden=/^(api_?key|token|refresh_?token|pairing_?key|password|authorization|secret)$/i;
        const check=value=>{if(value&&typeof value==='object')for(const [key,item] of Object.entries(value)){if(forbidden.test(key))throw Error('导入记录不能保存凭据');check(item);}};
        check(record);
        const all=this.imports(dirs),index=all.findIndex(r=>r.id===record.id);
        if(index<0)all.unshift(record);else all[index]=record;
        const f=this.importFile(dirs),temp=f+'.tmp';fs.writeFileSync(temp,JSON.stringify(all.slice(0,100)),{mode:0o600,flag:'wx'});fs.renameSync(temp,f);
        return {saved:true};
    }
    verifyImport(dirs,{extensionName,id}){
        if(typeof extensionName!=='string'||!/^[\w-]{1,80}$/.test(extensionName)||typeof id!=='string')throw Error('导入验证参数无效');
        const settings=JSON.parse(fs.readFileSync(path.join(dirs.root,'settings.json'),'utf8'));
        return {verified:!!settings.extension_settings?.[extensionName]?.cosji?.civitCompletedImports?.[id]};
    }
}
module.exports={Broker,targetUrl,SECRET_KEY};
