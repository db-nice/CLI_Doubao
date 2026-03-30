package org.jf.baksmali.sposkosm.tools;

// app/src/main/java/ma/dexter/tools/RenameClassTool.java

//import ma.dexter.dex.MutableDexFile;
//import ma.dexter.dex.MutableClassDef;
import ma.dexter.tools.smali.BaksmaliInvoker;
import ma.dexter.tools.smali.SmaliInvoker;
import ma.dexter.tasks.Result;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.immutable.ImmutableClassDef;

public class RenameClassTool {

    /**
     * 重命名类
     * @param dexFile DEX文件
     * @param oldClassName 旧类名（完整格式，如：Lm/A$B;）
     * @param newClassName 新类名（完整格式，如：Lm/A$C;）
     * @return 是否成功
     */
    public static boolean renameClass(MutableDexFile dexFile, String oldClassName, String newClassName) {
        try {
            // 1. 查找旧的类定义
            MutableClassDef oldClassDef = dexFile.findClassDef(oldClassName);
            if (oldClassDef == null) {
                return false;
            }

            // 2. 获取Smali代码
            BaksmaliInvoker baksmali = new BaksmaliInvoker();
            String smaliCode = baksmali.disassemble(oldClassDef);

            // 3. 替换类名
            String newSmaliCode = replaceClassNameInSmali(smaliCode, oldClassName, newClassName);

            // 4. 重新汇编
            Result<ClassDef> result = SmaliInvoker.assemble(newSmaliCode);
            if (!result.success) {
                return false;
            }

            // 5. 删除旧类，添加新类
            dexFile.deleteClassDef(oldClassDef);
            dexFile.addClassDef(result.value);

            return true;

        } catch (Exception e) {
            e.printStackTrace();
            return false;
        }
    }

    /**
     * 在Smali代码中替换类名
     */
    private static String replaceClassNameInSmali(String smaliCode, String oldClassName, String newClassName) {
        // 移除开头的L和结尾的;
        String oldSimpleName = oldClassName.substring(1, oldClassName.length() - 1);
        String newSimpleName = newClassName.substring(1, newClassName.length() - 1);

        // 替换.class指令
        String result = smaliCode.replace(
                ".class " + oldClassName,
                ".class " + newClassName
        );

        // 替换所有对该类的引用
        result = result.replace(oldClassName, newClassName);

        // 替换源文件引用（如果有）
        result = result.replace(
                ".source \"" + getSimpleClassName(oldSimpleName) + ".java\"",
                ".source \"" + getSimpleClassName(newSimpleName) + ".java\""
        );

        return result;
    }

    /**
     * 从完整类名获取简单类名
     * 如：m/A$B -> A$B
     */
    private static String getSimpleClassName(String fullClassName) {
        int lastSlash = fullClassName.lastIndexOf('/');
        if (lastSlash != -1) {
            return fullClassName.substring(lastSlash + 1);
        }
        return fullClassName;
    }

    /**
     * 创建新的类定义（用于完全新建类）
     */
    public static ClassDef createNewClassDef(String className, String superClass) {
        return new ImmutableClassDef(
                className,           // type
                0,                   // access flags
                superClass,          // superclass
                null,                // interfaces
                null,                // source file
                null,                // annotations
                null,                // fields
                null                 // methods
        );
    }
}