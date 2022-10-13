# iPhone Backpacker
Use python to copy images from iPhone on Windows OS is a struggle task, needless to say copying manually. iPhoneBackpacker can help you copy images in iPhone automatically by editing the config.  
  
</br>

## Prerequirements
Install below in your windows PC. The environment setting as:  
* Python==3.8 (3.8.10 fine)
* pywin32 (pywin32-304-cp38-cp38-win_amd64)
* tqdm==4.55.2
  
</br>


## Usage
Edit the config "EditThis.txt", and then run the program "iphoneCopyByConfig.py".  
</br>
Steps details:
1. Connect your iPhone to Windows PC.  
2. Copy the path of the iPhone image folder you want to backed up.  
3. Paste the path below the line "你想複製的資料夾們: (一個資料夾一行)"  
(You can paste multiple paths of folders for backup at the same time)
4. Copy the path of destination folder and paste below the line  
"你想複製到的母資料夾: (只放一個資料夾，上面的資料夾都會放在這個資料夾之下)"  
(You can only choose ONE folder as your destination.)
5. "The line "只複製圖檔嗎? (yes/no)"  
Yes if you want to copy images only.  
No if you want to copy all files (.jpg, .MOV, .AAE, etc).
6. Run "iphoneCopyByConfig.py"
  
</br>

## Example of config
EditThis.txt:
```
你想複製的資料夾們: (一個資料夾一行)
本機\Apple iPhone\Internal Storage\DCIM\100APPLE
本機\Apple iPhone\Internal Storage\DCIM\101APPLE
本機\Apple iPhone\Internal Storage\DCIM\102APPLE

你想複製到的母資料夾: (只放一個資料夾，上面的資料夾都會放在這個資料夾之下)
D:\3chdog\myimages

只複製圖檔嗎? (yes/no)
yes
```

## Credits
**[3chdog](https://github.com/3chdog/)**

[![GitHub](https://img.shields.io/github/followers/3chdog.svg?style=social&label=Follow%203chdog)](https://github.com/3chdog/)
