package org.jf.baksmali.sposkosm;

import org.jf.dexlib2.DexFileFactory;
import org.jf.dexlib2.Opcodes;
import org.jf.dexlib2.rewriter.DexRewriter;

import java.io.File;

import org.jf.dexlib2.iface.DexFile;
import org.jf.dexlib2.iface.Method;
import org.jf.dexlib2.iface.MethodImplementation;
import org.jf.dexlib2.iface.MethodParameter;
import org.jf.dexlib2.iface.reference.MethodReference;
import org.jf.dexlib2.iface.Annotation;
import org.jf.dexlib2.rewriter.Rewriter;
import org.jf.dexlib2.rewriter.RewriterModule;
import org.jf.dexlib2.rewriter.Rewriters;

import javax.annotation.Nonnull;
import java.io.IOException;
import java.util.List;
import java.util.Set;

public class DexModifier {

    static void testRewrite(String dexfilepath) {
        DexFile dexFile;
        try {
            // 把要修改的dex load进来
            dexFile = DexFileFactory.loadDexFile(dexfilepath, Opcodes.getDefault());
            System.out.println("dexFile: " + dexFile.getClass().getName());

            DexRewriter rewriter = new DexRewriter(new RewriterModule() {
                @Override
                public Rewriter<Method> getMethodRewriter(@Nonnull Rewriters rewriters) {
                    return new MyMethod();
                }
            });

            // 重写dex文件
            DexFile rewrittenDexFile = rewriter.rewriteDexFile(dexFile);
            File olddex = new File(dexfilepath);
            if (olddex.exists()) {
                System.out.println("delete original dex");
                olddex.delete();
            }

            // 生成新dex
            DexFileFactory.writeDexFile(dexfilepath, rewrittenDexFile);
            System.out.println("Dex file rewritten successfully");

        } catch (IOException e) {
            System.out.println("failed");
            e.printStackTrace();
        }
    }

    // 修改dex中的method
    static class MyMethod implements Rewriter<Method> {
        @Nonnull
        @Override
        public Method rewrite(@Nonnull final Method value) {
            // 找到 helloMethod
            if (value.getName().contains("helloMethod")) {
                System.out.println("rewrite: " + value.getName());
                return new Method() {
                    @Nonnull
                    @Override
                    public String getDefiningClass() {
                        return value.getDefiningClass();
                    }

                    @Nonnull
                    @Override
                    public String getName() {
                        // 将helloMethod重命名为MyMethod
                        return "MyMethod";
                    }

                    @Nonnull
                    @Override
                    public List<? extends CharSequence> getParameterTypes() {
                        return value.getParameterTypes();
                    }

                    @Nonnull
                    @Override
                    public String getReturnType() {
                        return value.getReturnType();
                    }

                    @Override
                    public int getAccessFlags() {
                        return value.getAccessFlags();
                    }

                    @Nonnull
                    @Override
                    public Set<? extends Annotation> getAnnotations() {
                        return value.getAnnotations();
                    }

                    @Nonnull
                    @Override
                    public Set<org.jf.dexlib2.HiddenApiRestriction> getHiddenApiRestrictions() {
                        return value.getHiddenApiRestrictions();
                    }

                    @Override
                    public MethodImplementation getImplementation() {
                        return value.getImplementation();
                    }

                    @Nonnull
                    @Override
                    public List<? extends MethodParameter> getParameters() {
                        return value.getParameters();
                    }

                    @Override
                    public int compareTo(MethodReference o) {
                        return value.compareTo(o);
                    }

                    // 【关键修复】新增 validateReference() 方法，直接复用原方法的逻辑
                    @Override
                    public void validateReference() throws InvalidReferenceException {
                        value.validateReference();
                    }
                };
            }
            return value;
        }
    }

    // 主方法用于测试
    public static void main(String[] args) {
        if (args.length > 0) {
            testRewrite(args[0]);
        } else {
            System.out.println("Usage: java DexModifier <dexfilepath>");
        }
    }
}