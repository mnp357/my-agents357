import fs from 'node:fs/promises';
import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(process.argv[2]));
const blob=await wb.render({sheetName:'Позиции',range:'A20:G29',scale:1,format:'png'});
await fs.writeFile('work/spacing-preview.png',new Uint8Array(await blob.arrayBuffer()));
