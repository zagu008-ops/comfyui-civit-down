const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {Broker}=require('./broker.cjs');
module.exports.info={id:'cosji-civit',name:'Cosji Civit Downloads',description:'Paired ComfyUI download proxy; pairing credentials stay in SillyTavern server secrets.'};
module.exports.init=async function(router){
    const secrets=await import(pathToFileURL(path.join(process.cwd(),'src/endpoints/secrets.js')).href);
    const broker=new Broker({secrets});
    function route(method,url,handler){router[method](url,async(req,res)=>{
        try{
            if(!req.user?.directories)return res.status(401).json({error:'请先登录酒馆'});
            const result=await handler(req.user.directories,req.body||{});
            res.json(result);
        }catch(error){res.status(400).json({error:error instanceof TypeError?'请求格式或目标连接无效':error.message});}
    });}
    route('get','/connections',dirs=>({connections:broker.list(dirs)}));
    route('post','/pair',(dirs,body)=>broker.pair(dirs,body));
    route('post','/proxy',(dirs,body)=>broker.proxy(dirs,body));
    route('post','/wiki',(_,body)=>broker.wiki(body.search));
    route('post','/preview',(_,body)=>broker.preview(body.url));
    route('get','/imports',dirs=>({imports:broker.imports(dirs)}));
    route('post','/imports',(dirs,body)=>broker.saveImport(dirs,body));
    route('post','/verify-import',(dirs,body)=>broker.verifyImport(dirs,body));
};
