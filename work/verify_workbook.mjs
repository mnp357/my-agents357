import fs from 'node:fs/promises';
import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(process.argv[2]));
console.log((await wb.inspect({kind:'sheet',include:'id,name'})).ndjson);
for(const name of ['Сводка','Позиции','Охват']){
 const blob=await wb.render({sheetName:name,range:name==='Сводка'?'A1:F16':name==='Позиции'?'A1:G10':'A1:F10',scale:1,format:'png'});
 await fs.writeFile('work/qa-'+name+'.png',new Uint8Array(await blob.arrayBuffer()));
}
