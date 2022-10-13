#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import iphoneCopyOneFolder as ipCopy

KEYWORDS = [u'你想複製的資料夾們', u'你想複製到的母資料夾', u'只複製圖檔嗎']
KEY_CHARACTER = [u'從', u'到']
NUMBERS='0123456789'
SUFFIX_POSITION=['FRONT', 'REAR'] # ['100APPLE' or 'APPLE100']


def readConfig():
    with open('EditThis.txt', 'r', encoding='utf-8') as file: ##### edit file path of .txt
        lines = file.readlines()
        lines = [line.rstrip() for line in lines]
    return lines

def parseWithCharacterAtFirst(readline):
    idx = 1
    while readline[idx]==" ": idx+=1
    return readline[0], readline[idx:]

def getNumAndSuffix(addr):
    folderName = os.path.basename(addr)
    num = ''.join((char if char in NUMBERS else ' ') for char in folderName)
    num = str(int(num)) # otherwise, split fail
    suffix = folderName.split(num)
    if suffix[0]=='': return int(num), suffix[-1], SUFFIX_POSITION[-1]
    else: return int(num), suffix[0], SUFFIX_POSITION[0]

def appendSequence(sequence_from, sequence_to, folderList):
    dirname_path = os.path.dirname(sequence_from)
    num_from, suffix, suffix_pos = getNumAndSuffix(sequence_from)
    num_to, _, _ = getNumAndSuffix(sequence_to)
    
    for idx in range(num_from, num_to+1):
        folderName = "{}{}".format(idx, suffix) if suffix_pos==SUFFIX_POSITION[-1] else "{}{}".format(suffix, idx)
        folderList.append(os.path.join(dirname_path, folderName))
    
    return folderList


def getArguments(fileLines):
    srcFolders = []
    dstDirFolder = None
    filtering = True
    store_lock = [False, False, False] # locks of KEYWORDS
    sequence_from = None

    for line in fileLines:
        print("\n\n==============\nnew For loop:\n{}\n{}".format(store_lock, line))
        if line == "":
            store_lock = [False, False, False]
            print("RESET all sign")
            continue
        
        # sequence-folder section
        if (store_lock[0]==True) and (line[0] in KEY_CHARACTER):
            keyChar, sequence_addr = parseWithCharacterAtFirst(line)
            if keyChar==KEY_CHARACTER[0]: sequence_from = sequence_addr
            elif keyChar==KEY_CHARACTER[-1]:
                assert isinstance(sequence_from, str)
                print(srcFolders)
                srcFolders = appendSequence(sequence_from, line, srcFolders)
                print(srcFolders)
                sequence_from = None
            continue

        # storing section
        if store_lock[0]==1: srcFolders.append(line)
        if store_lock[1]==1: dstDirFolder = line
        if store_lock[2]==1: filtering = True if line == u"yes" else False

        # Title section
        for i in range(len(KEYWORDS)):
            if KEYWORDS[i] in line:
                print(i)
                store_lock[i] = True
    
    assert os.path.exists(dstDirFolder)
    dstFolders = []
    for path in srcFolders:
        foldername = os.path.basename(path)
        dstFolders.append(os.path.join(dstDirFolder, foldername))

    print(srcFolders)
    print(dstFolders)
    print(filtering)
    return srcFolders, dstFolders, filtering

def mkfolders(dstFolders):
    for folder in dstFolders:
        if not os.path.exists(folder): os.makedirs(folder)

def main():
    fileLines = readConfig()
    print(fileLines)
    srcFolders, dstFolders, filtering = getArguments(fileLines)
    mkfolders(dstFolders)
    for src, dst in zip(srcFolders, dstFolders):
        print("\n\n\n[Copy] Now Copy \"{}\" to \"{}\"".format(src, dst))
        sourceFolderObject, _ = ipCopy.getFolderObject_byAbsPath(src)
        destinFolderObject, _ = ipCopy.getFolderObject_byAbsPath(dst)
        ipCopy.copyShellItem_batch(sourceFolderObject, destinFolderObject, sourceFolderObject, filtering=filtering)

if __name__=="__main__":
    main()
    
