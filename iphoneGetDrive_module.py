from win32com.shell import shell, shellcon
import pythoncom, os
from typing import List
FILTER=["jpg", "JPG", "Jpg",
        "jpeg", "JPEG", "Jpeg",
        "png", "PNG", "Png",
        "bmp", "BMP", "Bmp"]


def getItemsInside(parentFolderObject, filtered=False):
    nameList = []
    for pidl in parentFolderObject:
        fileName = parentFolderObject.GetDisplayNameOf(pidl, shellcon.SHGDN_NORMAL)
        if not filtered: nameList.append(fileName)
        else:
            extName = fileName.split(".")[-1]
            if extName in FILTER: nameList.append(fileName)
    return nameList

def ChineseCharacterChecking(folderName):
    if folderName == "Users": return "使用者"
    elif folderName == "Public": return "公用"
    elif folderName == "Desktop": return "桌面"
    elif folderName == "Pictures": return "圖片"
    elif folderName == "Videos": return "影片"
    elif folderName == "Downloads": return "下載"
    else: return folderName

def getFolderObject(folderName, parentFolderObject=None, isWholeName=True, characterChecking=True):
    if characterChecking: folderName = ChineseCharacterChecking(folderName)
    if parentFolderObject is None: parentFolderObject = shell.SHGetDesktopFolder()
    pidl_get = None
    for pidl in parentFolderObject:
        fileName = parentFolderObject.GetDisplayNameOf(pidl, shellcon.SHGDN_NORMAL)
        #print("===========", folderName, fileName)
        if isWholeName:
            if fileName == folderName:
                print("Get object: {}".format(folderName))
                pidl_get = pidl
                break
        else:
            if folderName in fileName:
                print("Get object with partial name \"{}\": {}".format(folderName, fileName))
                pidl_get = pidl
                break
    if pidl_get is None:
        print("Error: Cannot find \"{}\" in parentFolder".format(folderName))
        print(getItemsInside(parentFolderObject))
    folder = parentFolderObject.BindToObject(pidl_get, None, shell.IID_IShellFolder)
    return folder, pidl_get



def iter_split(s):
    dirFolders = []
    rest, tail = os.path.split(s)
    while tail != "":
        dirFolders.insert(0, tail)
        rest, tail = os.path.split(rest)
    return dirFolders



def checkFolderName(folderName:str):
    print("Checking folder name: {}".format(folderName))
    while "\\" in folderName:
        #print(folderName)
        folderName = folderName[1:]
    print("Finishing checking folder name: {}".format(folderName))
    return folderName



def parseAbsName(winAbsPath:str, rootName="本機"):
    pathSplit = winAbsPath.split(":")
    dirFolders = list(iter_split(pathSplit[-1]))
    #dirFolders[0] = checkFolderName(dirFolders[0])
    dirFolders.insert(0, pathSplit[0])
    dirFolders.insert(0, rootName)
    print("Parsing Complete: {}".format(dirFolders))
    return dirFolders

def getFolderObject_byAbsPath(absPath):
    print("Starting to get Object from absolute path: {}".format(absPath))
    dirFolders = parseAbsName(absPath)

    folderObject, _ = getFolderObject(dirFolders[0])
    folderObject, _ = getFolderObject(dirFolders[1]+":", folderObject,  isWholeName=False)

    for folder in dirFolders[2:]:
        parentFolderObject = folderObject
        folderObject, folderPIDL = getFolderObject(folder, parentFolderObject)

    print("Sucess to get folder object.\nObject with path: [{}]\n".format(absPath))
    return folderObject, folderPIDL
    


def copyShellItem(srcFolderObject, dstFolderObject, itemPIDL, dstItemName=None):
    src_idl = shell.SHGetIDListFromObject(srcFolderObject) #Grab the PIDL from the folder object
    dst_idl = shell.SHGetIDListFromObject(dstFolderObject) #Grab the PIDL from the folder object

    src = shell.SHCreateShellItem(src_idl, None, itemPIDL) #Create a ShellItem of the source file
    dst = shell.SHCreateItemFromIDList(dst_idl)

    # Python IFileOperation
    pfo = pythoncom.CoCreateInstance(shell.CLSID_FileOperation,None,pythoncom.CLSCTX_ALL,shell.IID_IFileOperation)
    pfo.SetOperationFlags(shellcon.FOF_NOCONFIRMATION)
    pfo.CopyItem(src, dst, dstItemName) # Schedule an operation to be performed
    success = pfo.PerformOperations() #perform operation
    print("Copy Log: {}".format(success))


def copyShellItem_batch(srcFolderObject, dstFolderObject, itemPIDL,itemPIDL2, dstItemName=None):
    src_idl = shell.SHGetIDListFromObject(srcFolderObject) #Grab the PIDL from the folder object
    dst_idl = shell.SHGetIDListFromObject(dstFolderObject) #Grab the PIDL from the folder object

    src = shell.SHCreateShellItem(src_idl, None, itemPIDL) #Create a ShellItem of the source file
    src2 = shell.SHCreateShellItem(src_idl, None, itemPIDL2) #Create a ShellItem of the source file
    dst = shell.SHCreateItemFromIDList(dst_idl)

    # Python IFileOperation
    pfo = pythoncom.CoCreateInstance(shell.CLSID_FileOperation,None,pythoncom.CLSCTX_ALL,shell.IID_IFileOperation)
    pfo.SetOperationFlags(shellcon.FOF_NOCONFIRMATION)
    pfo.CopyItem(src, dst, dstItemName) # Schedule an operation to be performed
    pfo.CopyItem(src2, dst, dstItemName) # Schedule an operation to be performed
    success = pfo.PerformOperations() #perform operation
    print("Copy Log: {}".format(success))



print("=================")
desktop = shell.SHGetDesktopFolder()
#getFolderObject("本機", isWholeName=False)
#a=  shell.SHCreateItemFromParsingName("D:\\python_file", None, shell.IID_IShellItem)
#print(a)
sourceFolderObject, _ = getFolderObject_byAbsPath("D:\\python_file\\image")
destinFolderObject, _ = getFolderObject_byAbsPath("C:\\Users\\Administrator\\Desktop\\pic\\ga")

#print(getItemsInside(sourceFolderObject))
#a = getItemsInside(sourceFolderObject, filtered=True)[0]
'''
for i in sourceFolderObject:
    copyShellItem(sourceFolderObject, destinFolderObject, i)
'''
for i, ss in enumerate(sourceFolderObject):
    if i==0: ppp = ss
    if i==1:
        ppp2 = ss
        break
copyShellItem(sourceFolderObject, destinFolderObject, ppp)
copyShellItem(sourceFolderObject, destinFolderObject, ppp2)

#copyShellItem(sourceFolderObject, destinFolderObject, ppp)

