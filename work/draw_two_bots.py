from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
W,H=1500,880
im=Image.new('RGB',(W,H),'#e8eef3'); d=ImageDraw.Draw(im)
regular='/System/Library/Fonts/Supplemental/Arial.ttf'
bold='/System/Library/Fonts/Supplemental/Arial Bold.ttf'
F=lambda size,b=False:ImageFont.truetype(bold if b else regular,size)
d.text((80,48),'Два отдельных чата в Telegram',font=F(42,True),fill='#172b38')
d.text((80,104),'Макет: второй бот будет создан и подключён после вашего просмотра',font=F(23),fill='#526575')

def phone(x,title,handle,color,task,response,commands):
 y=165; w=640; h=650
 d.rounded_rectangle((x,y,x+w,y+h),radius=26,fill='white',outline='#d7e1e8',width=2)
 d.rounded_rectangle((x,y,x+w,y+105),radius=26,fill='#ffffff')
 d.rectangle((x,y+78,x+w,y+106),fill='#ffffff')
 d.line((x,y+105,x+w,y+105),fill='#e2e8ed',width=2)
 d.ellipse((x+24,y+23,x+80,y+79),fill=color)
 d.text((x+43,y+31),title[0],font=F(31,True),fill='white')
 d.text((x+99,y+26),title,font=F(27,True),fill='#1b303e')
 d.text((x+99,y+61),handle,font=F(18),fill='#7c8c98')
 d.rounded_rectangle((x+28,y+137,x+430,y+201),radius=18,fill='#ecf3f7')
 d.text((x+49,y+154),task,font=F(22),fill='#244456')
 d.rounded_rectangle((x+113,y+229,x+607,y+312),radius=18,fill='#d8f4df')
 d.text((x+139,y+249),'/activate',font=F(24,True),fill='#225b3f')
 d.rounded_rectangle((x+28,y+340,x+577,y+467),radius=18,fill='#ecf3f7')
 for n,line in enumerate(response):d.text((x+51,y+360+n*35),line,font=F(22),fill='#244456')
 d.line((x,y+552,x+w,y+552),fill='#e2e8ed',width=2)
 d.text((x+28,y+579),'Доступные команды',font=F(19,True),fill='#738797')
 d.text((x+28,y+612),commands,font=F(19),fill='#2b596d')
phone(80,'Hyperliquid','@Fkwer357_bot','#256d78','Ликвидации до 7% от цены',['Собираю CoinGlass.','Excel пришлю в этот чат.'],'/activate   /status   /pause')
phone(780,'Bybit — аналитик','отдельный бот','#7157a6','Сделки и движение средств',['Анализирую торговлю и переводы.','Excel пришлю в этот чат.'],'/activate   /status   /pause')
out=Path('outputs/Два-бота-Telegram-макет.png')
im.save(out)
print(out)
