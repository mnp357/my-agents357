import fs from 'node:fs/promises';
import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load('outputs/summary-preview/Hyperliquid-summary-preview.xlsx'));
const blob=await wb.render({sheetName:'Сводка',range:'A10:H19',scale:1.3,format:'png'});
await fs.writeFile('outputs/summary-preview/summary-preview.png',new Uint8Array(await blob.arrayBuffer()));
