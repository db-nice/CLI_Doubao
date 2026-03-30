package org.jf.baksmali.sposkosm;

import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.dexbacked.DexBackedDexFile;
import org.jf.dexlib2.iface.DexFile;
import org.jf.dexlib2.DexFileFactory;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.rewriter.DexRewriter;
import org.jf.dexlib2.rewriter.Rewriter;
import org.jf.dexlib2.rewriter.RewriterModule;
import org.jf.dexlib2.rewriter.Rewriters;

import javax.annotation.Nonnull;
import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

public class DexUtils {

    public static List getClassListFromDex(String dexFilePath) throws IOException {
                List classNames = new ArrayList();

                File dexFile = new File(dexFilePath);
        if (!dexFile.exists()) {
            throw new IOException("DEX文件不存在：" + dexFilePath);
        }

                DexFile dex = DexFileFactory.loadDexFile(dexFile, org.jf.dexlib2.Opcodes.forApi(25));                  for (ClassDef classDef : dex.getClasses()) {
            classNames.add(classDef.getType());          }

        return classNames;
    }
    public static void processClassesStream(String dexFilePath) throws IOException {

        File dexFile = new File(dexFilePath);
        if (!dexFile.exists()) {
            throw new IOException("DEX文件不存在：" + dexFilePath);
        }

                DexFile dex = DexFileFactory.loadDexFile(dexFile, org.jf.dexlib2.Opcodes.forApi(25));  
                Iterator<ClassDef> iterator = (Iterator<ClassDef>) dex.getClasses().iterator();
        while (iterator.hasNext()) {
            ClassDef classDef = iterator.next();
                        System.out.println(classDef.getType());          }
    }
    public static Iterator rak(Object factoryObj) throws IOException {

                DexFile dex = (DexFile) factoryObj;

                return dex.getClasses().iterator();
    }
    public static void main(String[] args) {
        try {
                        String dexPath = "F:\\Project\\Android\\exnop\\dex-editor-master\\smali-master\\build\\dex\\classes.dex";
            processClassesStream(dexPath);
        } catch (IOException e) {
            e.printStackTrace();
        }
    }
}
